"""The money side of a small fleet: things owners lose today because nobody tracks them.

1. DETENTION  — GPS proves arrival/departure at loading & unloading sites -> waited hours -> detention claim
                (free hours and ₹/day per fleet; defaults 4 h and ₹1,500/day or part thereof).
2. PAYMENTS   — delivery -> POD photo -> invoice (freight + detention - advance) -> due date -> reminders the owner
                can forward; "kis party se kitna baaki" any time; payments marked by voice.
3. DRIVER     — the driver's own ledger: salary accrues monthly, bata per trip day, advances given, payments made;
                "mera kitna baaki hai?" by voice; salary-day reminder to the owner.
4. E-WAY BILL — validity vs ETA (remaining km at ~40 km/h truck average) -> warn before it expires so it can be
                extended (extension is allowed from 8 h before to 8 h after expiry).
5. SERVICE    — odometer from GPS km -> service / tyre reminders by km.
6. E-CHALLAN  — pending challans per truck: manual entry, or a licensed provider API (CHALLAN_API_URL/KEY).
7. RETURN LOAD— "load mesh": owners post loads they can't serve; when a truck is near its destination the CEO Agent
                finds loads starting near there and checks profit with the cost engine.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, timedelta, timezone

import httpx

from . import costing, db, geo
from .agents.common import inr, ist_now, mask_reg
from .bus import bus, to_driver

log = logging.getLogger("saarthi.business")

SITE_RADIUS_KM = 1.5
LEAVE_RADIUS_KM = 2.5
TRUCK_AVG_KMPH = 40
RETURN_LOAD_SEARCH_KM = 60
NEAR_DEST_KM = 120


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _once(key: str, every: timedelta | None = None) -> bool:
    """True if this reminder has not been sent (within `every`)."""
    row = db.one("SELECT sent_at FROM reminders_sent WHERE key=?", (key,))
    if row and (every is None or _now() - datetime.fromisoformat(row["sent_at"]) < every):
        return False
    db.ex("INSERT OR REPLACE INTO reminders_sent(key, sent_at) VALUES(?,?)", (key, db.now()))
    return True


async def _owner(v_or_fleet: dict, text: str, speak: str | None = None) -> None:
    from .agents import ceo
    f = v_or_fleet if "owner_chat_id" in v_or_fleet else db.fleet(v_or_fleet["fleet_id"])
    o = ceo.owner_out(f, text, speak=speak)
    if o:
        await bus.send(o)


# =============================== 1. DETENTION ===============================
def detention_amount(waited_h: float, free_h: float, per_day: float) -> float:
    excess = waited_h - free_h
    return 0.0 if excess <= 0 else per_day * math.ceil(excess / 24)


def _sites(t: dict) -> list[tuple[str, float, float]]:
    out = []
    if t.get("origin_lat") is not None:
        out.append(("loading: " + (t["origin"] or "origin"), t["origin_lat"], t["origin_lon"]))
    if t.get("dest_lat") is not None:
        out.append(("unloading: " + (t["destination"] or "destination"), t["dest_lat"], t["dest_lon"]))
    return out


async def on_location(v: dict, t: dict | None, moving: bool) -> None:
    if not t or v.get("last_lat") is None:
        return
    lat, lon = v["last_lat"], v["last_lon"]
    open_visit = db.one("SELECT * FROM site_visits WHERE vehicle_id=? AND left_at IS NULL ORDER BY id DESC LIMIT 1", (v["id"],))
    if open_visit:
        site = next((s for s in _sites(t) if s[0] == open_visit["site"]), None)
        if site and geo.haversine_km(lat, lon, site[1], site[2]) > LEAVE_RADIUS_KM:
            await close_visit(v, t, open_visit)
        return
    if moving:
        return
    for name, slat, slon in _sites(t):
        if geo.haversine_km(lat, lon, slat, slon) <= SITE_RADIUS_KM:
            db.ex("INSERT INTO site_visits(vehicle_id,trip_id,site,arrived_at,source) VALUES(?,?,?,?, 'gps')",
                  (v["id"], t["id"], name, db.now()))
            db.log_agent("ceo:detention", f"driver:{v['code']}", "site_arrived", {"site": name})
            return


async def close_visit(v: dict, t: dict, visit: dict) -> dict:
    f = db.fleet(v["fleet_id"])
    arrived = datetime.fromisoformat(visit["arrived_at"])
    waited = (_now() - arrived).total_seconds() / 3600
    amount = detention_amount(waited, f["detention_free_h"] or 4, f["detention_per_day"] or 1500)
    db.ex("UPDATE site_visits SET left_at=?, waited_h=?, detention_amount=? WHERE id=?",
          (db.now(), round(waited, 2), amount, visit["id"]))
    if amount:
        await _owner(v, f"⏱️ Detention claim — {v['reg_label']} at {visit['site']}: waited {waited:.1f} h "
                        f"(free {f['detention_free_h']:g} h) → claim {inr(amount)}. GPS proof: arrived "
                        f"{arrived.astimezone(ist_now().tzinfo):%d %b %H:%M}, left {ist_now():%d %b %H:%M}. "
                        f"Added to this trip's invoice.")
        inv = db.one("SELECT * FROM invoices WHERE trip_id=?", (t["id"],))
        if inv:
            db.ex("UPDATE invoices SET detention=detention+?, amount_due=amount_due+? WHERE id=?", (amount, amount, inv["id"]))
    return {"site": visit["site"], "waited_h": round(waited, 1), "detention": amount}


async def mark_site(v: dict, event: str, site: str = "") -> dict:
    """Driver says 'loading point pe pahunch gaya' / 'nikal gaye' (backup to GPS)."""
    t = db.active_trip(v["id"])
    if not t:
        return {"error": "no active trip"}
    open_visit = db.one("SELECT * FROM site_visits WHERE vehicle_id=? AND left_at IS NULL ORDER BY id DESC LIMIT 1", (v["id"],))
    if event == "arrived":
        if open_visit:
            return {"already": open_visit["site"]}
        name = site or ("unloading: " + t["destination"] if t.get("delivered_at") is None and v.get("last_lat") is not None
                        and t.get("dest_lat") is not None
                        and geo.haversine_km(v["last_lat"], v["last_lon"], t["dest_lat"], t["dest_lon"]) < 30
                        else "loading: " + (t["origin"] or "origin"))
        db.ex("INSERT INTO site_visits(vehicle_id,trip_id,site,arrived_at,source) VALUES(?,?,?,?, 'voice')",
              (v["id"], t["id"], name, db.now()))
        return {"arrived": name, "say": "Note kar liya, waiting time ginna shuru. 4 ghante ke baad detention banega."}
    if open_visit:
        return await close_visit(v, t, open_visit)
    return {"error": "no open site visit"}


# =============================== 2. PAYMENTS ===============================
async def delivered(v: dict, receiver: str | None = None) -> dict:
    t = db.active_trip(v["id"])
    if not t:
        return {"error": "no active trip"}
    open_visit = db.one("SELECT * FROM site_visits WHERE vehicle_id=? AND left_at IS NULL", (v["id"],))
    db.ex("UPDATE trips SET delivered_at=? WHERE id=?", (db.now(), t["id"]))
    f = db.fleet(v["fleet_id"])
    advance = db.one("SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE trip_id=? AND category='freight_advance' "
                     "AND status='confirmed'", (t["id"],))["s"]
    detention = db.one("SELECT COALESCE(SUM(detention_amount),0) s FROM site_visits WHERE trip_id=?", (t["id"],))["s"]
    freight = t["freight_amount"] or 0
    if not db.one("SELECT id FROM invoices WHERE trip_id=?", (t["id"],)):
        db.ex("INSERT INTO invoices(fleet_id,trip_id,party,freight,detention,advance_received,amount_due,status) "
              "VALUES(?,?,?,?,?,?,?, 'awaiting_pod')",
              (f["id"], t["id"], t["client_name"] or "Client", freight, detention, advance, freight + detention - advance))
    await bus.send(to_driver(v, "📸 POD ki photo lijiye (receiving copy / bilty)", actions=[{"type": "pod", "trip_id": t["id"]}]))
    await _owner(v, f"📦 Delivered — {v['reg_label']} {t['origin']} → {t['destination']}"
                    + (f", received by {receiver}" if receiver else "") + f". Balance due {inr(freight + detention - advance)}"
                    f" ({inr(freight)} freight + {inr(detention)} detention − {inr(advance)} advance). Waiting for POD photo.")
    return {"say": "Badhai ho boss, delivery ho gayi! Gaadi khadi ho to bilty ya receiving ki ek photo le lijiye, "
                   "screen pe button hai. Usse paisa jaldi aayega.",
            "detention_open": bool(open_visit)}


def pod_received(trip_id: int, filename: str) -> dict | None:
    inv = db.one("SELECT * FROM invoices WHERE trip_id=?", (trip_id,))
    if not inv:
        return None
    f = db.fleet(inv["fleet_id"])
    due = (date.today() + timedelta(days=f["payment_terms_days"] or 30)).isoformat()
    db.ex("UPDATE invoices SET pod_file=?, status='due', issued_at=?, due_at=? WHERE id=?",
          (filename, db.now(), due, inv["id"]))
    db.ex("UPDATE trips SET status='closed', ended_at=? WHERE id=?", (db.now(), trip_id))
    return db.one("SELECT * FROM invoices WHERE id=?", (inv["id"],))


def invoice_text(inv: dict) -> str:
    t = db.one("SELECT t.*, v.reg_label FROM trips t JOIN vehicles v ON v.id=t.vehicle_id WHERE t.id=?", (inv["trip_id"],))
    f = db.fleet(inv["fleet_id"])
    lines = [f"INVOICE #{inv['id']} — {f['name']}", f"To: {inv['party']}",
             f"Trip: {t['origin']} → {t['destination']} · Vehicle {t['reg_label']}",
             f"Freight: {inr(inv['freight'])}"]
    if inv["detention"]:
        lines.append(f"Detention (GPS-verified waiting): {inr(inv['detention'])}")
    if inv["advance_received"]:
        lines.append(f"Less advance received: -{inr(inv['advance_received'])}")
    lines.append(f"Balance due: {inr(inv['amount_due'] - inv['paid'])}" + (f" by {inv['due_at']}" if inv["due_at"] else ""))
    return "\n".join(lines)


def receivables(fleet_id: int) -> dict:
    rows = db.q("SELECT * FROM invoices WHERE fleet_id=? AND status IN ('awaiting_pod','due','overdue','part_paid')", (fleet_id,))
    today = date.today().isoformat()
    by_party: dict[str, float] = {}
    out = []
    for r in rows:
        bal = r["amount_due"] - r["paid"]
        by_party[r["party"]] = by_party.get(r["party"], 0) + bal
        out.append({"invoice": r["id"], "party": r["party"], "balance": bal, "due_at": r["due_at"], "status": r["status"],
                    "overdue_days": (date.today() - date.fromisoformat(r["due_at"])).days if r["due_at"] and r["due_at"] < today else 0})
    return {"total_receivable": sum(by_party.values()), "by_party": by_party, "invoices": out}


def record_payment(fleet_id: int, party: str, amount: float) -> dict:
    """Owner: 'Sharma Logistics se 20,000 aa gaye' -> applied to that party's oldest invoices."""
    left = amount
    applied = []
    for inv in db.q("SELECT * FROM invoices WHERE fleet_id=? AND lower(party) LIKE lower(?) AND status NOT IN ('paid') "
                    "ORDER BY id", (fleet_id, f"%{party}%")):
        if left <= 0:
            break
        bal = inv["amount_due"] - inv["paid"]
        pay = min(bal, left)
        left -= pay
        status = "paid" if pay >= bal else "part_paid"
        db.ex("UPDATE invoices SET paid=paid+?, status=? WHERE id=?", (pay, status, inv["id"]))
        applied.append({"invoice": inv["id"], "paid": pay, "status": status})
    return {"applied": applied, "unapplied": left}


async def payment_reminders() -> None:
    today = date.today()
    for inv in db.q("SELECT * FROM invoices WHERE status IN ('due','overdue','part_paid') AND due_at IS NOT NULL"):
        due = date.fromisoformat(inv["due_at"])
        days = (today - due).days
        if days < -2:
            continue
        if days > 0 and inv["status"] == "due":
            db.ex("UPDATE invoices SET status='overdue' WHERE id=?", (inv["id"],))
        if not _once(f"payrem:{inv['id']}", timedelta(days=3)):
            continue
        bal = inv["amount_due"] - inv["paid"]
        when = "kal due hai" if days == -1 else ("aaj due hai" if days == 0 else f"{days} din se overdue hai")
        msg = (f"Namaste {inv['party']} ji, invoice #{inv['id']} ka {inr(bal)} {when}. POD aur GPS detention proof "
               f"saath hai. Kripya payment kar dijiye. Dhanyavaad. — {db.fleet(inv['fleet_id'])['name']}")
        db.ex("UPDATE invoices SET last_reminder_at=?, reminders=reminders+1 WHERE id=?", (db.now(), inv["id"]))
        await _owner(db.fleet(inv["fleet_id"]), f"💰 Payment follow-up ready (forward to {inv['party']}):\n\n{msg}")


# =============================== 3. DRIVER LEDGER ===============================
def ledger_add(vid: int, kind: str, amount: float, note: str = "") -> None:
    db.ex("INSERT INTO driver_ledger(vehicle_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",
          (vid, kind, amount, note, db.now()))


def driver_balance(vid: int) -> dict:
    rows = db.q("SELECT kind, SUM(amount) s FROM driver_ledger WHERE vehicle_id=? GROUP BY kind", (vid,))
    k = {r["kind"]: r["s"] for r in rows}
    earned = k.get("salary", 0) + k.get("bata", 0) + k.get("bonus", 0)
    taken = k.get("advance", 0) + k.get("paid", 0)
    return {"earned": earned, "advances": k.get("advance", 0), "paid": k.get("paid", 0), "balance_due_to_driver": earned - taken,
            "detail": k}


async def ledger_tick() -> None:
    today = ist_now().date()
    for v in db.q("SELECT * FROM vehicles WHERE driver_name IS NOT NULL"):
        # bata per day on an active trip
        if db.active_trip(v["id"]) and _once(f"bata:{v['id']}:{today}"):
            ledger_add(v["id"], "bata", v["driver_bata_day"] or 400, f"bata {today}")
        # monthly salary on the 1st + reminder to owner
        if today.day == 1 and _once(f"salary:{v['id']}:{today:%Y-%m}"):
            ledger_add(v["id"], "salary", v["driver_salary_month"] or 18000, f"salary {today:%b %Y}")
            bal = driver_balance(v["id"])["balance_due_to_driver"]
            await _owner(v, f"🧑‍✈️ Salary day — {v['driver_name']} ({mask_reg(v['reg_label'])}): balance due {inr(bal)}. "
                            "Time pe salary = driver tikta hai.")


# =============================== 4. E-WAY BILL ===============================
def eway_validity(generated: datetime, distance_km: float, over_dimensional: bool = False) -> datetime:
    """Validity: 1 day per 200 km (20 km for over-dimensional cargo); each day runs to midnight of the following day,
    so a 1-day bill generated on 4 Nov is valid till 5 Nov 23:59."""
    per = 20 if over_dimensional else 200
    days = max(1, math.ceil(distance_km / per))
    g = generated.astimezone(ist_now().tzinfo)
    return (g + timedelta(days=days)).replace(hour=23, minute=59, second=0, microsecond=0)


async def eway_check() -> None:
    for t in db.q("SELECT * FROM trips WHERE status='active' AND eway_valid_until IS NOT NULL AND delivered_at IS NULL"):
        v = db.vehicle(t["vehicle_id"])
        if v["last_lat"] is None or t["dest_lat"] is None:
            continue
        remaining_km = geo.haversine_km(v["last_lat"], v["last_lon"], t["dest_lat"], t["dest_lon"]) * 1.25  # road factor
        eta = _now() + timedelta(hours=remaining_km / TRUCK_AVG_KMPH)
        valid = datetime.fromisoformat(t["eway_valid_until"])
        if eta > valid - timedelta(hours=2) and _once(f"eway:{t['id']}"):
            msg = (f"📄 E-way bill {t['eway_bill'] or ''} for {v['reg_label']} expires {valid.astimezone(ist_now().tzinfo):%d %b %H:%M}, "
                   f"but ETA is ~{eta.astimezone(ist_now().tzinfo):%d %b %H:%M} ({remaining_km:.0f} km left). Extend it on the "
                   "GST e-way bill portal (allowed 8 h before to 8 h after expiry) to avoid a penalty.")
            await _owner(v, msg)
            await bus.send(to_driver(v, msg, speak="Boss, e-way bill time pe khatam ho jayega. Malik ko bata diya hai, woh extend karwa denge."))


# =============================== 5. SERVICE BY KM ===============================
def add_km(vid: int, km: float) -> None:
    db.ex("UPDATE vehicles SET odometer_km=COALESCE(odometer_km,0)+? WHERE id=?", (km, vid))


async def service_check() -> None:
    for v in db.q("SELECT * FROM vehicles"):
        odo = v["odometer_km"] or 0
        for kind, last, interval, label in (("service", v["last_service_km"], v["service_interval_km"], "service"),
                                            ("tyre", v["last_tyre_km"], v["tyre_interval_km"], "tyre check / rotation")):
            if not interval:
                continue
            left = (last or 0) + interval - odo
            if left <= 500 and _once(f"{kind}:{v['id']}:{int(((last or 0) + interval) // 1000)}"):
                txt = (f"🔧 {v['reg_label']}: {label} due in {max(0, left):.0f} km (odometer {odo:.0f} km)."
                       if left > 0 else f"🔧 {v['reg_label']}: {label} OVERDUE by {-left:.0f} km.")
                await _owner(v, txt)
                await bus.send(to_driver(v, txt, speak=f"Boss, gaadi ka {label} {'aane wala hai' if left > 0 else 'overdue hai'}. "
                                                       "Agle bade shehar mein karwa lein?"))


def service_done(vid: int, kind: str = "service", odometer: float | None = None) -> dict:
    v = db.vehicle(vid)
    odo = odometer if odometer is not None else (v["odometer_km"] or 0)
    col = "last_tyre_km" if kind == "tyre" else "last_service_km"
    db.ex(f"UPDATE vehicles SET {col}=?, odometer_km=MAX(COALESCE(odometer_km,0), ?) WHERE id=?", (odo, odo, vid))
    return {"recorded": kind, "at_km": odo}


# =============================== 6. E-CHALLAN ===============================
async def fetch_challans(v: dict) -> list[dict]:
    """Licensed provider hook. CHALLAN_API_URL receives {"vehicle_number": ...} with a Bearer CHALLAN_API_KEY and must
    return a JSON list of {challan_no, offence, amount, date, status}. Adapt `_map` to your provider's format."""
    url, key = os.getenv("CHALLAN_API_URL"), os.getenv("CHALLAN_API_KEY")
    if not url or not key:
        return []
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, json={"vehicle_number": v["reg_label"].replace(" ", "")}, headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        data = r.json()
    items = data if isinstance(data, list) else data.get("data") or data.get("challans") or []
    return [_map(x) for x in items if isinstance(x, dict)]


def _map(x: dict) -> dict:
    return {"challan_no": str(x.get("challan_no") or x.get("challan_number") or x.get("id")),
            "offence": x.get("offence") or x.get("offense") or x.get("violation") or "",
            "amount": float(x.get("amount") or x.get("fine") or 0), "issued_on": str(x.get("date") or x.get("challan_date") or ""),
            "status": (x.get("status") or "pending").lower()}


def add_challan(vid: int, challan_no: str, amount: float, offence: str = "", issued_on: str = "", source: str = "manual") -> None:
    db.ex("INSERT OR IGNORE INTO challans(vehicle_id,challan_no,offence,amount,issued_on,source) VALUES(?,?,?,?,?,?)",
          (vid, challan_no, offence, amount, issued_on, source))


async def challan_check() -> None:
    if ist_now().weekday() != 0:  # Mondays
        return
    for v in db.q("SELECT * FROM vehicles"):
        if not _once(f"challan:{v['id']}:{ist_now():%Y-%W}"):
            continue
        try:
            for c in await fetch_challans(v):
                add_challan(v["id"], c["challan_no"], c["amount"], c["offence"], c["issued_on"], "api")
        except Exception as e:
            log.warning("challan fetch failed for %s: %s", v["code"], e)
        pend = db.q("SELECT * FROM challans WHERE vehicle_id=? AND status='pending'", (v["id"],))
        if pend:
            await _owner(v, f"🚓 {v['reg_label']}: {len(pend)} pending challan(s), total {inr(sum(p['amount'] for p in pend))}. "
                            "Pay on echallan.parivahan.gov.in before they block fitness/permit renewal.")


# =============================== 7. RETURN LOADS (load mesh) ===============================
async def post_load(fleet_id: int, origin: str, destination: str, freight: float, weight_t: float | None = None,
                    vtype: str | None = None, ready_on: str | None = None) -> dict:
    o, d = await geo.geocode(origin), await geo.geocode(destination)
    if not o or not d:
        return {"error": "place not found on map"}
    lid = db.ex("INSERT INTO loads(fleet_id,origin,destination,o_lat,o_lon,d_lat,d_lon,weight_t,vtype,freight,ready_on,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (fleet_id, origin, destination, o["lat"], o["lon"], d["lat"], d["lon"], weight_t, vtype, freight, ready_on, db.now()))
    return {"load_id": lid, "shared_with": "all Saarthi fleets with mesh on"}


def loads_near(lat: float, lon: float, exclude_fleet: int | None = None, radius_km: float = RETURN_LOAD_SEARCH_KM) -> list[dict]:
    out = []
    for l in db.q("SELECT l.*, f.name fleet_name FROM loads l JOIN fleets f ON f.id=l.fleet_id WHERE l.status='open'"):
        if exclude_fleet and l["fleet_id"] == exclude_fleet:
            continue
        dkm = geo.haversine_km(lat, lon, l["o_lat"], l["o_lon"])
        if dkm <= radius_km:
            l["pickup_km"] = round(dkm)
            out.append(l)
    return sorted(out, key=lambda x: x["pickup_km"])


def load_profit(load: dict, v: dict) -> dict:
    distance = geo.haversine_km(load["o_lat"], load["o_lon"], load["d_lat"], load["d_lon"]) * 1.25
    f = db.fleet(v["fleet_id"])
    est = costing.estimate(costing.CostInputs(distance_km=distance + load.get("pickup_km", 0), vtype=v["vtype"],
                                              diesel_price=f["diesel_price"], offered_freight=load["freight"],
                                              load_tonnes=load["weight_t"]))
    return {"approx_km": round(distance), "cost": est["total_cost"], "profit": est.get("profit_at_offer"),
            "margin_pct": est.get("margin_at_offer_pct"), "warnings": est["warnings"]}


async def return_load_check() -> None:
    for t in db.q("SELECT * FROM trips WHERE status='active' AND dest_lat IS NOT NULL"):
        v = db.vehicle(t["vehicle_id"])
        if v["last_lat"] is None:
            continue
        if geo.haversine_km(v["last_lat"], v["last_lon"], t["dest_lat"], t["dest_lon"]) > NEAR_DEST_KM:
            continue
        loads = loads_near(t["dest_lat"], t["dest_lon"])
        if not loads or not _once(f"retload:{t['id']}"):
            continue
        lines = []
        for l in loads[:3]:
            p = load_profit(l, v)
            lines.append(f"• #{l['id']} {l['origin']} → {l['destination']} {inr(l['freight'])}"
                         f"{' · ' + str(l['weight_t']) + ' t' if l['weight_t'] else ''} ({l['fleet_name']}) — est. profit "
                         f"{inr(p['profit'])} ({p['margin_pct']}%)")
        await _owner(v, f"🔁 Return load for {v['reg_label']} (reaching {t['destination']} soon):\n" + "\n".join(lines)
                        + "\nSay 'load #ID le lo' to take it.")


async def accept_load(load_id: int, v: dict) -> dict:
    l = db.one("SELECT * FROM loads WHERE id=?", (load_id,))
    if not l or l["status"] != "open":
        return {"error": "load no longer available"}
    db.ex("UPDATE loads SET status='taken', taken_by_vehicle=? WHERE id=? AND status='open'", (v["id"], load_id))
    poster = db.fleet(l["fleet_id"])
    await _owner(poster, f"✅ Your load #{load_id} {l['origin']} → {l['destination']} was taken by "
                         f"{db.fleet(v['fleet_id'])['name']} ({mask_reg(v['reg_label'])}). They will contact you on the mesh.")
    return {"taken": load_id, "next_trip": f"{l['origin']} → {l['destination']}", "freight": l["freight"]}


# =============================== periodic ===============================
async def tick() -> None:
    for job in (payment_reminders, ledger_tick, eway_check, service_check, challan_check, return_load_check):
        try:
            await job()
        except Exception:
            log.exception("business job %s failed", job.__name__)


def summary(fleet_id: int) -> dict:
    rec = receivables(fleet_id)
    since = (_now() - timedelta(days=30)).isoformat(timespec="seconds")
    det = db.one("SELECT COALESCE(SUM(s.detention_amount),0) a, COUNT(*) n FROM site_visits s JOIN vehicles v ON v.id=s.vehicle_id "
                 "WHERE v.fleet_id=? AND s.arrived_at>=? AND s.detention_amount>0", (fleet_id, since))
    drivers = [{"vehicle": v["code"], "driver": v["driver_name"], **driver_balance(v["id"])}
               for v in db.q("SELECT * FROM vehicles WHERE fleet_id=? AND driver_name IS NOT NULL", (fleet_id,))]
    service = [{"vehicle": v["code"], "odometer_km": round(v["odometer_km"] or 0),
                "service_due_in_km": round((v["last_service_km"] or 0) + (v["service_interval_km"] or 0) - (v["odometer_km"] or 0))}
               for v in db.q("SELECT * FROM vehicles WHERE fleet_id=?", (fleet_id,))]
    challans = db.q("SELECT v.code, c.challan_no, c.amount, c.offence FROM challans c JOIN vehicles v ON v.id=c.vehicle_id "
                    "WHERE v.fleet_id=? AND c.status='pending'", (fleet_id,))
    loads = db.q("SELECT id, origin, destination, freight, status FROM loads WHERE fleet_id=? ORDER BY id DESC LIMIT 10", (fleet_id,))
    return {"receivables": rec, "detention_30d": {"claims": det["n"], "amount": det["a"]}, "drivers": drivers,
            "service": service, "pending_challans": challans, "my_posted_loads": loads}
