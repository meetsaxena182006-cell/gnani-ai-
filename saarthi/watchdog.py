"""CEO Agent's eye: watches every truck, every GPS fix, and makes sure nothing is missed.

Rules (deterministic; thresholds below):
- TOLL: GPS track crosses an NHAI toll plaza -> recorded with the expected fee for this vehicle class.
  If 2 crossings are unreported (or 1 for 45 min), the CEO Agent asks the driver through the companion:
  "Driver sahab, Paduna toll cross kiya — kitna laga? FASTag ya cash?". A toll entry clears them.
- LONG STOP: stopped > 45 min on an active trip with no explanation -> companion asks if all is well;
  no answer to that check -> owner alerted (via the fatigue/check-in escalation).
- GOING AWAY FROM DESTINATION: distance to destination grows by > 15 km while moving -> companion asks.
- OVERSPEED: > 80 km/h -> gentle companion warning (max once per 20 min), counted in the owner's report.
- COMPANION / GPS OFFLINE: no GPS for 30 min on an active trip -> owner alerted once.
- DIESEL CHECK: on each diesel entry, GPS km since the last fill vs litres -> implied km/L; far below the truck's
  profile -> flagged to the owner (possible leak, theft or wrong entry).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import business, db, geo, tolls
from .agents import ceo
from .agents.common import inr
from .bus import bus, to_driver
from .config import settings
from .costing import PROFILES

log = logging.getLogger("saarthi.watchdog")

TOLL_ASK_AFTER = 2            # unreported crossings
TOLL_SINGLE_WAIT_MIN = 45
LONG_STOP_MIN = 45
AWAY_KM = 15
OVERSPEED_KMPH = float(80)
OFFLINE_MIN = 30
DIESEL_KMPL_RATIO = 0.7       # flag if implied km/L < 70% of the profile

_stopped_since: dict[int, datetime] = {}
_asked_stop: set[int] = set()
_min_dest_km: dict[int, float] = {}
_asked_away: dict[int, datetime] = {}
_last_speed_warn: dict[int, datetime] = {}
_offline_alerted: set[int] = set()
_pump_stop: dict[int, dict] = {}       # vehicle_id -> {"name", "since", "lat", "lon"}
_pump_checked: dict[int, tuple] = {}   # vehicle_id -> rounded position already looked up during this stop
PUMP_RADIUS_M = 150
PUMP_MIN_STOP_MIN = 4


async def _pump_here(lat: float, lon: float) -> dict | None:
    try:
        res = await geo.nearby("fuel", lat, lon, radius_m=PUMP_RADIUS_M, limit=1)
    except Exception as e:
        log.warning("fuel lookup failed: %s", e)
        return None
    return res[0] if res else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def event(vid: int, kind: str, detail: str) -> None:
    db.ex("INSERT INTO watch_events(vehicle_id,kind,detail,created_at) VALUES(?,?,?,?)", (vid, kind, detail, db.now()))
    db.log_agent("ceo:watchdog", f"vehicle:{vid}", kind, {"detail": detail})


async def ask_driver(v: dict, text: str, kind: str) -> None:
    """CEO Agent -> Driver Agent: the companion asks the driver (spoken, added to the conversation)."""
    db.log_agent(f"ceo:{v['fleet_id']}", f"driver:{v['code']}", f"watchdog_{kind}", {"ask": text})
    await bus.send(to_driver(v, f"🏢 {text}", speak=text))


# ---------------- per GPS fix ----------------
async def on_location(v_before: dict, v_after: dict, speed_kmph: float | None) -> None:
    vid = v_after["id"]
    t = db.active_trip(vid)
    prev = (v_before["last_lat"], v_before["last_lon"]) if v_before.get("last_lat") is not None else None
    cur = (v_after["last_lat"], v_after["last_lon"])
    _offline_alerted.discard(vid)
    if t and prev:
        seg = geo.haversine_km(prev[0], prev[1], cur[0], cur[1])
        if seg < 50:  # ignore GPS jumps
            db.ex("UPDATE trips SET gps_km=COALESCE(gps_km,0)+? WHERE id=?", (seg, t["id"]))
    if prev:
        seg_all = geo.haversine_km(prev[0], prev[1], cur[0], cur[1])
        if seg_all < 50:
            business.add_km(vid, seg_all)
    # tolls
    for p in tolls.crossed(prev, cur):
        recent = db.one("SELECT id FROM toll_crossings WHERE vehicle_id=? AND plaza=? AND crossed_at>=?",
                        (vid, p["name"], (_now() - timedelta(hours=6)).isoformat(timespec="seconds")))
        if recent:
            continue
        fee = tolls.expected_fee(p, v_after["vtype"])
        cid = db.ex("INSERT INTO toll_crossings(vehicle_id,trip_id,plaza,lat,lon,expected_fee,crossed_at) VALUES(?,?,?,?,?,?,?)",
                    (vid, t["id"] if t else None, p["name"], p["lat"], p["lon"], fee, db.now()))
        event(vid, "toll_crossed", f"{p['name']} (expected ≥ {inr(fee)})" if fee else p["name"])
        if v_after.get("fastag", 1) and fee:
            # FASTag pays automatically: log the toll now (estimate), replace with the actual deduction when the
            # FASTag statement / SMS arrives (see fastag.py). The driver is not disturbed.
            eid = db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,note,lat,lon,status,created_at) "
                        "VALUES(?,?, 'toll', ?, ?, ?, ?, 'confirmed', ?)",
                        (vid, t["id"] if t else None, fee, f"FASTag {p['name']} (estimated)", p["lat"], p["lon"], db.now()))
            db.ex("UPDATE toll_crossings SET status='fastag_estimated', expense_id=? WHERE id=?", (eid, cid))
    # stops
    moving = speed_kmph is not None and speed_kmph >= settings.moving_speed_kmph
    if moving:
        _stopped_since.pop(vid, None)
        _asked_stop.discard(vid)
        _pump_checked.pop(vid, None)
        stop = _pump_stop.pop(vid, None)
        if stop and t and _now() - stop["since"] >= timedelta(minutes=PUMP_MIN_STOP_MIN):
            filled = db.one("SELECT id FROM expenses WHERE vehicle_id=? AND category='diesel' AND created_at>=? "
                            "AND status IN ('pending','confirmed')", (vid, stop["since"].isoformat(timespec="seconds")))
            mins = int((_now() - stop["since"]).total_seconds() // 60)
            event(vid, "fuel_stop", f"{stop['name']} ({mins} min){'' if filled else ' — diesel not logged'}")
            if not filled:
                await ask_driver(v_after, f"Boss, {stop['name']} pump pe {mins} minute ruke the. Diesel bharwaya? "
                                          f"Kitne litre aur kitne rupaye ka?", "fuel")
    else:
        _stopped_since.setdefault(vid, _now())
        key = (round(cur[0], 3), round(cur[1], 3))
        if vid not in _pump_stop and _pump_checked.get(vid) != key:
            _pump_checked[vid] = key
            pump = await _pump_here(*cur)
            if pump:
                _pump_stop[vid] = {"name": pump["name"], "since": _stopped_since[vid], "lat": pump["lat"], "lon": pump["lon"]}
    # detention: arrival/departure at loading & unloading sites
    await business.on_location(v_after, t, moving)
    # overspeed
    if speed_kmph and speed_kmph > OVERSPEED_KMPH:
        last = _last_speed_warn.get(vid)
        if not last or _now() - last > timedelta(minutes=20):
            _last_speed_warn[vid] = _now()
            event(vid, "overspeed", f"{speed_kmph:.0f} km/h")
            await ask_driver(v_after, f"Boss, speed {speed_kmph:.0f} hai. Thoda aaram se, ghar pe koi intezaar kar raha hai.", "overspeed")
    # moving away from destination
    if t and t.get("dest_lat") is not None and moving:
        d = geo.haversine_km(cur[0], cur[1], t["dest_lat"], t["dest_lon"])
        best = _min_dest_km.get(t["id"])
        if best is None or d < best:
            _min_dest_km[t["id"]] = d
        elif d - best > AWAY_KM:
            last = _asked_away.get(vid)
            if not last or _now() - last > timedelta(hours=1):
                _asked_away[vid] = _now()
                event(vid, "away_from_destination", f"{d:.0f} km from {t['destination']} (was {best:.0f})")
                await ask_driver(v_after, f"Boss, {t['destination']} se doori badh rahi hai. Rasta badla hai ya koi kaam hai?", "route")


# ---------------- periodic checks ----------------
async def tick() -> None:
    now = _now()
    for v in db.q("SELECT * FROM vehicles"):
        vid = v["id"]
        t = db.active_trip(vid)
        if not t:
            continue
        await _toll_check(v, t)
        # long stop
        since = _stopped_since.get(vid)
        open_inc = db.one("SELECT id FROM incidents WHERE vehicle_id=? AND status='open'", (vid,))
        if since and vid not in _asked_stop and not open_inc and now - since > timedelta(minutes=LONG_STOP_MIN):
            _asked_stop.add(vid)
            mins = int((now - since).total_seconds() // 60)
            near = tolls.nearest(v["last_lat"], v["last_lon"], 30) if v["last_lat"] is not None else None
            where = f" {near[0]['name']} ke paas" if near else ""
            event(vid, "long_stop", f"{mins} min{where}")
            await ask_driver(v, f"Boss, gaadi{where} {mins} minute se ruki hai. Sab theek? Aaraam kar rahe ho ya koi dikkat hai?", "stop")
        # GPS / companion offline on an active trip
        if v["last_seen"] and vid not in _offline_alerted:
            gap = now - datetime.fromisoformat(v["last_seen"])
            if gap > timedelta(minutes=OFFLINE_MIN):
                _offline_alerted.add(vid)
                event(vid, "offline", f"no GPS for {int(gap.total_seconds() // 60)} min")
                f = db.fleet(v["fleet_id"])
                o = ceo.owner_out(f, f"📵 {v['driver_name'] or 'Driver'} ({v['reg_label']}): no location for "
                                     f"{int(gap.total_seconds() // 60)} min on an active trip "
                                     f"({t['origin']} → {t['destination']}). Network or phone off? Please call.")
                if o:
                    await bus.send(o)


async def _toll_check(v: dict, t: dict) -> None:
    vid = v["id"]
    # reported tolls clear earlier crossings (driver may report several plazas in one amount)
    last_toll = db.one("SELECT id, created_at FROM expenses WHERE vehicle_id=? AND category='toll' AND status='confirmed' "
                       "AND COALESCE(note,'') NOT LIKE 'FASTag%' ORDER BY id DESC LIMIT 1", (vid,))
    if last_toll:
        db.ex("UPDATE toll_crossings SET status='reported', expense_id=? WHERE vehicle_id=? AND status IN ('unreported','asked') "
              "AND crossed_at<=?", (last_toll["id"], vid, last_toll["created_at"]))
    pending = db.q("SELECT * FROM toll_crossings WHERE vehicle_id=? AND status='unreported' ORDER BY id", (vid,))
    if not pending:
        return
    oldest = datetime.fromisoformat(pending[0]["crossed_at"])
    if len(pending) >= TOLL_ASK_AFTER or _now() - oldest > timedelta(minutes=TOLL_SINGLE_WAIT_MIN):
        names = " aur ".join(p["plaza"] for p in pending[:3])
        exp = sum(p["expected_fee"] or 0 for p in pending)
        hint = f" Hamare hisaab se kam se kam {inr(exp)} hona chahiye." if exp else ""
        text = f"Driver sahab, {names} toll cross kiya. Kitna toll laga? FASTag se kata ya cash?{hint}"
        db.ex(f"UPDATE toll_crossings SET status='asked', asked_at=? WHERE id IN ({','.join('?' * len(pending))})",
              (db.now(), *[p["id"] for p in pending]))
        event(vid, "toll_unreported", names)
        await ask_driver(v, text, "toll")


# ---------------- diesel sanity ----------------
async def on_diesel(v: dict, litres: float | None, amount: float) -> None:
    """Called when a diesel entry is confirmed: compare GPS km since the previous fill with litres."""
    if not litres:
        return
    t = db.active_trip(v["id"])
    if not t:
        return
    prev = db.one("SELECT created_at FROM expenses WHERE vehicle_id=? AND category='diesel' AND status='confirmed' "
                  "AND trip_id=? ORDER BY id DESC LIMIT 1 OFFSET 1", (v["id"], t["id"]))
    km = t.get("gps_km") or 0
    if not prev or km < 150:
        return  # first fill of the trip or too little distance to judge
    profile = PROFILES.get(v["vtype"], PROFILES["truck_10w"])
    implied = km / litres
    if implied < profile.kmpl_loaded * DIESEL_KMPL_RATIO:
        detail = f"{km:.0f} km on {litres:g} L = {implied:.1f} km/L (expected ~{profile.kmpl_loaded} km/L)"
        event(v["id"], "diesel_low_efficiency", detail)
        f = db.fleet(v["fleet_id"])
        o = ceo.owner_out(f, f"⛽ Diesel check — {v['reg_label']}: {detail}. Possible leak, wrong entry or diesel loss.")
        if o:
            await bus.send(o)


def report(fleet_id: int, since_iso: str) -> list[str]:
    rows = db.q("SELECT w.kind, COUNT(*) n FROM watch_events w JOIN vehicles v ON v.id=w.vehicle_id "
                "WHERE v.fleet_id=? AND w.created_at>=? GROUP BY w.kind", (fleet_id, since_iso))
    labels = {"toll_crossed": "toll plazas crossed", "toll_unreported": "toll reminders", "overspeed": "overspeed warnings",
              "long_stop": "long stops checked", "away_from_destination": "route deviations", "offline": "offline alerts",
              "diesel_low_efficiency": "diesel flags"}
    return [f"{labels.get(r['kind'], r['kind'])}: {r['n']}" for r in rows]
