"""CEO Sentinel: the CEO Agent's always-on checklist. Its job is to prove that NOTHING is missing.

The watchdog (watchdog.py) reacts to single GPS fixes. The Sentinel looks at every truck as a whole, every
minute, and answers one question for the owner: "is everything going smoothly, and if not, what exactly is
missing?" Each truck gets a checklist; each check is ok / warn / bad with a plain reason.

It also CLOSES THE LOOP on things the CEO Agent already asked the driver about:
- toll asked, still not answered after FOLLOWUP_MIN -> the CEO asks the Driver Agent again (once);
- still nothing after the second ask -> the owner is told, with the plazas and the expected amount;
- driving too long without a break -> the companion suggests chai + rest (never pushes to speed up);
- a newly 'bad' check (expired e-way bill, expired document on an active trip, 8 h+ driving) -> owner, once.

All thresholds are rules in code (never decided by the LLM), so the result is the same every time.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from . import db
from .agents import ceo
from .agents.common import inr
from .bus import bus, to_driver
from .costing import PROFILES

log = logging.getLogger("saarthi.sentinel")

GPS_STALE_MIN = 5
GPS_OFFLINE_MIN = 30
FOLLOWUP_MIN = 30          # re-ask an unanswered toll question after this long
BREAK_AFTER_H = 4.5        # MTW Act: rest within 5 h of driving
LIMIT_H = 8.0              # MTW Act: 8 h driving a day
FUEL_RANGE_WARN = 0.85     # km since last fill > 85% of a full tank's range -> diesel probably not logged

OK, WARN, BAD = "ok", "warn", "bad"
_RANK = {OK: 0, WARN: 1, BAD: 2}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mins_since(iso: str | None) -> float | None:
    if not iso:
        return None
    return (_now() - datetime.fromisoformat(iso)).total_seconds() / 60


def _check(key: str, label: str, status: str, detail: str) -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail}


# ---------------- per-truck checklist ----------------
def audit_vehicle(v: dict) -> dict:
    vid = v["id"]
    t = db.active_trip(vid)
    checks: list[dict] = []

    # 1. location: the CEO must always know where the truck is
    gap = _mins_since(v["last_seen"])
    if gap is None:
        checks.append(_check("gps", "Location", WARN if not t else BAD, "never shared"))
    elif gap <= GPS_STALE_MIN:
        checks.append(_check("gps", "Location", OK, "live"))
    elif gap <= GPS_OFFLINE_MIN or not t:
        checks.append(_check("gps", "Location", WARN, f"last fix {gap:.0f} min ago"))
    else:
        checks.append(_check("gps", "Location", BAD, f"no GPS for {gap:.0f} min on an active trip"))

    # 2. companion (Driver Agent) reachable
    reachable = vid in bus.live or bool(v.get("driver_chat_id"))
    checks.append(_check("companion", "Driver Agent",
                         OK if vid in bus.live else (WARN if reachable or not t else BAD),
                         "live in the cab" if vid in bus.live else ("Telegram only" if reachable else "unreachable")))

    # 3. open breakdown / emergency
    inc = db.one("SELECT severity, description FROM incidents WHERE vehicle_id=? AND status='open' ORDER BY id DESC LIMIT 1", (vid,))
    checks.append(_check("incident", "Incidents", BAD if inc else OK,
                         f"{inc['severity']}: {inc['description']}" if inc else "none open"))

    # 4. driving hours (health + law)
    if v.get("driving_since"):
        h = (_now() - datetime.fromisoformat(v["driving_since"])).total_seconds() / 3600
        st = BAD if h >= LIMIT_H else WARN if h >= BREAK_AFTER_H else OK
        checks.append(_check("hours", "Driving hours", st, f"{h:.1f} h without a rest"))
    else:
        checks.append(_check("hours", "Driving hours", OK, "rested"))

    # 5. alertness check-ins answered
    missed = v.get("missed_checks") or 0
    checks.append(_check("checkins", "Check-ins", WARN if missed else OK,
                         f"{missed} unanswered" if missed else "answered"))

    if t:
        # 6. tolls: every NHAI plaza the GPS crossed must be accounted for
        rows = db.q("SELECT plaza, status, expected_fee FROM toll_crossings WHERE trip_id=?", (t["id"],))
        open_ = [r for r in rows if r["status"] in ("unreported", "asked")]
        if not rows:
            checks.append(_check("tolls", "Tolls", OK, "no plazas crossed yet"))
        elif open_:
            exp = sum(r["expected_fee"] or 0 for r in open_)
            checks.append(_check("tolls", "Tolls", WARN if len(open_) < 3 else BAD,
                                 f"{len(open_)} of {len(rows)} not reported ({', '.join(r['plaza'] for r in open_[:3])})"
                                 + (f", expected ≥ {inr(exp)}" if exp else "")))
        else:
            checks.append(_check("tolls", "Tolls", OK, f"all {len(rows)} accounted for"))

        # 7. diesel: has the truck driven further than a tank allows without a logged fill?
        p = PROFILES.get(v["vtype"], PROFILES["truck_10w"])
        tank_range = p.tank_l * p.kmpl_loaded
        last = db.one("SELECT created_at, odometer_km FROM expenses WHERE trip_id=? AND category='diesel' "
                      "AND status='confirmed' ORDER BY id DESC LIMIT 1", (t["id"],))
        km = t.get("gps_km") or 0
        if not last:
            km_since = km
        elif last.get("odometer_km") is not None and v.get("odometer_km") is not None:
            km_since = max(0.0, v["odometer_km"] - last["odometer_km"])
        else:
            km_since = _km_since(t, last["created_at"])
        if km_since > tank_range * FUEL_RANGE_WARN:
            checks.append(_check("diesel", "Diesel", WARN, f"{km_since:.0f} km without a logged fill "
                                                           f"(a full tank does ~{tank_range:.0f} km)"))
        else:
            checks.append(_check("diesel", "Diesel", OK, "logged" if last else f"{km:.0f} km so far, within one tank"))

        # 8. money entries the driver started but never confirmed
        pend = db.one("SELECT COUNT(*) n FROM expenses WHERE vehicle_id=? AND status='pending'", (vid,))["n"]
        if pend:
            checks.append(_check("entries", "Unconfirmed entries", WARN, f"{pend} waiting for the driver's haan"))

        # 9. freight agreed
        checks.append(_check("freight", "Freight", OK if t.get("freight_amount") else WARN,
                             inr(t["freight_amount"]) if t.get("freight_amount") else "not set, profit unknown"))

        # 10. e-way bill validity
        if t.get("eway_valid_until"):
            left_h = (datetime.fromisoformat(t["eway_valid_until"]) - _now()).total_seconds() / 3600
            st = BAD if left_h <= 0 else WARN if left_h < 6 else OK
            checks.append(_check("eway", "E-way bill", st, "EXPIRED" if left_h <= 0 else f"valid {left_h:.0f} h more"))
        else:
            checks.append(_check("eway", "E-way bill", WARN, "not recorded"))

    # 11. documents
    today = date.today()
    docs = db.q("SELECT doc_type, expiry_date FROM documents WHERE vehicle_id=?", (vid,))
    expired = [d["doc_type"] for d in docs if d["expiry_date"] < today.isoformat()]
    soon = [d["doc_type"] for d in docs if today.isoformat() <= d["expiry_date"] <= (today + timedelta(days=15)).isoformat()]
    if expired:
        checks.append(_check("documents", "Documents", BAD if t else WARN, "expired: " + ", ".join(expired)))
    elif soon:
        checks.append(_check("documents", "Documents", WARN, "expiring in 15 days: " + ", ".join(soon)))
    else:
        checks.append(_check("documents", "Documents", OK, "valid"))

    worst = max((c["status"] for c in checks), key=_RANK.get)
    return {"vehicle": v["code"], "reg": v["reg_label"], "driver": v["driver_name"],
            "trip": f"{t['origin']} → {t['destination']}" if t else None,
            "status": worst, "checks": checks,
            "attention": [f"{c['label']}: {c['detail']}" for c in checks if c["status"] != OK]}


def _km_since(t: dict, since_iso: str) -> float:
    """Fallback for entries saved before odometer readings were recorded: estimate from the share of trip
    time elapsed since the fill."""
    start = datetime.fromisoformat(t["started_at"])
    since = datetime.fromisoformat(since_iso)
    total = max(1.0, (_now() - start).total_seconds())
    frac = max(0.0, min(1.0, (_now() - since).total_seconds() / total))
    return (t.get("gps_km") or 0) * frac


def fleet_health(fleet_id: int) -> dict:
    trucks = [audit_vehicle(v) for v in db.q("SELECT * FROM vehicles WHERE fleet_id=? ORDER BY code", (fleet_id,))]
    n_bad = sum(1 for t in trucks if t["status"] == BAD)
    n_warn = sum(1 for t in trucks if t["status"] == WARN)
    total_checks = sum(len(t["checks"]) for t in trucks)
    ok_checks = sum(1 for t in trucks for c in t["checks"] if c["status"] == OK)
    if not trucks:
        headline = "No trucks registered."
    elif n_bad:
        headline = f"⚠️ {n_bad} truck(s) need you now."
    elif n_warn:
        headline = f"🟡 Running, {sum(len(t['attention']) for t in trucks)} small thing(s) to tidy up."
    else:
        headline = f"✅ Sab theek: all {len(trucks)} trucks running smoothly, nothing missing."
    return {"all_clear": bool(trucks) and not n_bad and not n_warn, "headline": headline,
            "score_pct": round(100 * ok_checks / total_checks) if total_checks else 100,
            "checked_at": db.now(), "trucks": trucks}


# ---------------- closing the loop (runs every minute) ----------------
def _once(key: str) -> bool:
    """True the first time a key is seen (persisted, so restarts don't repeat alerts)."""
    if db.one("SELECT key FROM reminders_sent WHERE key=?", (key,)):
        return False
    db.ex("INSERT INTO reminders_sent(key, sent_at) VALUES(?,?)", (key, db.now()))
    return True


async def tick() -> None:
    from . import watchdog
    for v in db.q("SELECT * FROM vehicles"):
        t = db.active_trip(v["id"])
        f = db.fleet(v["fleet_id"])
        if t:
            await _toll_followup(v, t, f, watchdog)
        a = audit_vehicle(v)
        checks = {c["key"]: c for c in a["checks"]}
        # long driving: the companion suggests a break once per driving session (from driving_since)
        hours = checks.get("hours")
        if hours and hours["status"] != OK and v.get("driving_since"):
            if _once(f"sentinel:break:{v['id']}:{v['driving_since']}:{hours['status']}"):
                text = ("Boss, kaafi der se lagatar chala rahe ho. Agle dhabe pe 30 minute chai aur aaram ho jaaye? "
                        "Time ki chinta mat karo, main malik ko bata deta hoon.")
                await watchdog.ask_driver(v, text, "rest")
        # newly bad checks go to the owner once a day
        for key in ("eway", "documents", "hours", "gps"):
            c = checks.get(key)
            if c and c["status"] == BAD and _once(f"sentinel:{key}:{v['id']}:{date.today().isoformat()}"):
                watchdog.event(v["id"], f"sentinel_{key}", c["detail"])
                if key == "gps":
                    continue  # watchdog.tick already alerts the owner for offline trucks
                o = ceo.owner_out(f, f"👁️ CEO Sentinel — {v['reg_label']} ({v['driver_name'] or 'driver'}): "
                                     f"{c['label']} — {c['detail']}.")
                if o:
                    await bus.send(o)


async def _toll_followup(v: dict, t: dict, f: dict, watchdog) -> None:
    asked = db.q("SELECT * FROM toll_crossings WHERE trip_id=? AND status='asked' ORDER BY id", (t["id"],))
    if not asked:
        return
    mins = _mins_since(asked[-1]["asked_at"]) or 0
    if mins < FOLLOWUP_MIN:
        return
    times = max(r.get("asks") or 1 for r in asked)
    names = " aur ".join(r["plaza"] for r in asked[:3])
    exp = sum(r["expected_fee"] or 0 for r in asked)
    ids = [r["id"] for r in asked]
    marks = ",".join("?" * len(ids))
    if times < 2:
        # second ask, phrased like a person who noticed it was forgotten
        text = (f"Driver sahab, ek baat reh gayi — {names} toll ka kitna laga tha? Bas amount bol dijiye, "
                f"main likh leta hoon.")
        db.ex(f"UPDATE toll_crossings SET asked_at=?, asks=2 WHERE id IN ({marks})", (db.now(), *ids))
        watchdog.event(v["id"], "toll_followup", names)
        await watchdog.ask_driver(v, text, "toll_followup")
    elif _once(f"sentinel:toll_escalate:{min(ids)}"):
        watchdog.event(v["id"], "toll_escalated", names)
        db.log_agent(f"ceo:{f['code']}", "owner", "toll_escalated", {"plazas": names, "expected": exp})
        o = ceo.owner_out(f, f"🧾 {v['reg_label']} ({v['driver_name'] or 'driver'}) crossed {names} but has not told "
                             f"the toll amount after 2 reminders" + (f" (expected ≥ {inr(exp)})" if exp else "")
                             + ". Please check with the driver or the FASTag statement.")
        if o:
            await bus.send(o)
