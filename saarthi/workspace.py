"""Clean, real workspace: an owner (or a judge) creates an empty fleet and feeds it real business data —
trucks + drivers, orders/deals from customers, and today's trip plan. Nothing here is demo data.

The same functions back the office "Setup" screens, the CSV import and the CEO Agent's tools
("Udaipur se Surat 20 ton 45000 Sharma ka order aaya", "MW-T2 ko Surat bhejo")."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import secrets

from . import db, geo
from .bus import bus, to_driver

log = logging.getLogger("saarthi.workspace")


def _code(name: str) -> str:
    base = re.sub(r"[^A-Z]", "", (name or "FLEET").upper())[:4] or "FLT"
    while True:
        c = f"{base}-{secrets.randbelow(9000) + 1000}"
        if not db.one("SELECT 1 FROM fleets WHERE code=?", (c,)):
            return c


def create_fleet(name: str, owner_name: str = "", diesel_price: float = 90.0) -> dict:
    name = (name or "").strip()[:60] or "My Transport"
    code = _code(name)
    fid = db.ex("INSERT INTO fleets(code,name,owner_name,diesel_price,kind) VALUES(?,?,?,?, 'real')",
                (code, name, (owner_name or "").strip()[:40], diesel_price or 90.0))
    db.ex("UPDATE fleets SET owner_chat_id=900000+id WHERE id=? AND owner_chat_id IS NULL", (fid,))
    db.log_agent("setup", f"ceo:{code}", "fleet_created", {"name": name})
    return {"code": code, "name": name, "id": fid}


def fleet_by_code(code: str) -> dict | None:
    return db.one("SELECT * FROM fleets WHERE upper(code)=upper(?)", (code or "",))


async def add_vehicle(fleet: dict, reg: str, driver: str = "", home: str = "", vtype: str = "truck_10w",
                      mileage: float | None = None, insurance_till: str | None = None, puc_till: str | None = None) -> dict:
    reg = re.sub(r"\s+", " ", (reg or "").strip().upper())[:20]
    if not reg:
        return {"error": "truck number required"}
    n = db.one("SELECT COUNT(*) n FROM vehicles WHERE fleet_id=?", (fleet["id"],))["n"] + 1
    prefix = fleet["code"].split("-")[0][:4]
    code = f"{prefix}{fleet['id']}-T{n}"
    while db.vehicle_by_code(code):
        n += 1
        code = f"{prefix}{fleet['id']}-T{n}"
    tok = secrets.token_urlsafe(9)
    vid = db.ex("INSERT INTO vehicles(fleet_id,code,reg_label,vtype,driver_name,access_token,mileage_loaded) VALUES(?,?,?,?,?,?,?)",
                (fleet["id"], code, reg, vtype or "truck_10w", (driver or "").strip()[:40] or None, tok, mileage or 3.5))
    db.ex("UPDATE vehicles SET driver_chat_id=910000+id WHERE id=? AND driver_chat_id IS NULL", (vid,))
    where = None
    if home:
        try:
            where = await asyncio.wait_for(geo.geocode(home), 12)
        except Exception:
            where = None
        if where:
            db.ex("UPDATE vehicles SET last_lat=?, last_lon=?, last_speed_kmph=0, last_seen=? WHERE id=?",
                  (where["lat"], where["lon"], db.now(), vid))
    for doc, till in (("insurance", insurance_till), ("PUC", puc_till)):
        if till:
            db.ex("INSERT INTO documents(vehicle_id,doc_type,expiry_date) VALUES(?,?,?)", (vid, doc, till[:10]))
    db.log_agent("setup", f"ceo:{fleet['code']}", "truck_added", {"vehicle": code, "reg": reg, "driver": driver})
    from .config import settings
    return {"code": code, "reg": reg, "driver": driver, "located": bool(where),
            "driver_link": f"{settings.public_base_url}/driver?v={code}&k={tok}"}


async def add_order(fleet: dict, origin: str, destination: str, freight: float | None = None, weight_t: float | None = None,
                    party: str = "", ready_on: str | None = None, vtype: str | None = None) -> dict:
    """A customer order / deal the fleet has to carry (own order: shows in the fleet load plan)."""
    try:
        o, d = await asyncio.wait_for(asyncio.gather(geo.geocode(origin), geo.geocode(destination)), 15)
    except Exception:
        o = d = None
    if not o or not d:
        return {"error": f"could not find {'origin' if not o else 'destination'} on the map — use a city name"}
    lid = db.ex("INSERT INTO loads(fleet_id,origin,destination,o_lat,o_lon,d_lat,d_lon,weight_t,vtype,freight,ready_on,status,"
                "need,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?,?)",
                (fleet["id"], origin.strip(), destination.strip(), o["lat"], o["lon"], d["lat"], d["lon"], weight_t, vtype,
                 freight, ready_on, f"party:{party.strip()}" if party else None, db.now()))
    db.log_agent("setup", f"ceo:{fleet['code']}", "order_added",
                 {"load_id": lid, "route": f"{origin} → {destination}", "freight": freight, "party": party})
    return {"load_id": lid, "origin": origin, "destination": destination, "freight": freight, "weight_t": weight_t,
            "party": party}


def _vehicle_in(fleet: dict, key: str) -> dict | None:
    key = (key or "").strip().upper()
    for v in db.q("SELECT * FROM vehicles WHERE fleet_id=? ORDER BY code", (fleet["id"],)):
        if key and (key in (v["code"].upper(), v["reg_label"].upper()) or key in v["reg_label"].upper().replace(" ", "")
                    or (v["driver_name"] and key.lower() == v["driver_name"].lower().split()[0])):
            return v
    return None


async def assign_trip(fleet: dict, vehicle: str, origin: str = "", destination: str = "", freight: float | None = None,
                      client: str = "", weight_t: float | None = None, load_id: int | None = None) -> dict:
    """Today's plan: give a truck a trip (or an open order). The driver is told in the cab, a route plan is made."""
    v = _vehicle_in(fleet, vehicle)
    if not v:
        return {"error": f"no truck '{vehicle}' in {fleet['name']}"}
    if load_id:
        l = db.one("SELECT * FROM loads WHERE id=? AND fleet_id=? AND status='open'", (load_id, fleet["id"]))
        if not l:
            return {"error": f"open order #{load_id} not found"}
        origin, destination, freight, weight_t = l["origin"], l["destination"], l["freight"], l["weight_t"]
        client = (l.get("need") or "").removeprefix("party:") or client
        db.ex("UPDATE loads SET status='taken', taken_by_vehicle=? WHERE id=?", (v["id"], load_id))
    if not destination:
        return {"error": "destination required"}
    t = db.start_trip(v["id"], origin or "current location", destination, freight, client or None)
    try:
        o = await asyncio.wait_for(geo.geocode(origin), 12) if origin else None
        d = await asyncio.wait_for(geo.geocode(destination), 12)
    except Exception:
        o, d = None, None
    if o:
        db.ex("UPDATE trips SET origin_lat=?, origin_lon=? WHERE id=?", (o["lat"], o["lon"], t["id"]))
        if v["last_lat"] is None:
            db.ex("UPDATE vehicles SET last_lat=?, last_lon=?, last_speed_kmph=0, last_seen=? WHERE id=?",
                  (o["lat"], o["lon"], db.now(), v["id"]))
    if d:
        db.ex("UPDATE trips SET dest_lat=?, dest_lon=? WHERE id=?", (d["lat"], d["lon"], t["id"]))
    db.ex("UPDATE trips SET weight_t=? WHERE id=?", (weight_t, t["id"]))
    msg = (f"🏢 Office: aaj ka trip — {origin or 'yahan'} se {destination}" + (f", {weight_t:g} ton" if weight_t else "")
           + (f", party {client}" if client else "") + ". Plan bana ke aapko bata raha hoon.")
    await bus.send(to_driver(db.vehicle(v["id"]), msg))
    db.log_agent(f"ceo:{fleet['code']}", f"driver:{v['code']}", "trip_assigned",
                 {"route": f"{origin} → {destination}", "freight": freight, "client": client})

    async def _plan():
        from . import planner
        try:
            await planner.plan_for_vehicle(db.vehicle(v["id"]))
        except Exception:
            log.exception("plan after assign failed")
    asyncio.create_task(_plan())
    return {"trip_id": t["id"], "vehicle": v["code"], "reg": v["reg_label"], "route": f"{origin} → {destination}",
            "driver_told": True, "plan": "route plan is being made (arrives in the office in ~30 s)"}


async def import_orders_csv(fleet: dict, text: str) -> dict:
    """Business data in: a CSV export from Tally / Excel / Google Sheets with columns like
    origin,destination,weight_t,freight,party,date (header names are matched loosely)."""
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    alias = {"origin": ("origin", "from", "source", "pickup", "loading"), "destination": ("destination", "to", "drop", "unloading"),
             "weight_t": ("weight", "ton", "tonnes", "weight_t", "qty"), "freight": ("freight", "rate", "amount", "bhada", "price"),
             "party": ("party", "client", "customer", "consignor"), "date": ("date", "ready", "ready_on", "pickup_date")}

    def pick(r: dict, k: str):
        for col, val in r.items():
            if col and any(a in col.strip().lower() for a in alias[k]):
                return (val or "").strip()
        return ""

    def num(x: str):
        try:
            return float(re.sub(r"[^\d.]", "", x)) if x and re.search(r"\d", x) else None
        except ValueError:
            return None
    added, failed = [], []
    for r in rows[:50]:
        o, d = pick(r, "origin"), pick(r, "destination")
        if not o or not d:
            failed.append(r)
            continue
        res = await add_order(fleet, o, d, num(pick(r, "freight")), num(pick(r, "weight_t")), pick(r, "party"), pick(r, "date") or None)
        (failed if "error" in res else added).append(res)
    return {"rows": len(rows), "added": len(added), "failed": len(failed), "orders": added}


def snapshot(fleet: dict) -> dict:
    """Everything the office needs to show for one fleet's business data."""
    loads = db.q("SELECT * FROM loads WHERE fleet_id=? AND status='open' ORDER BY id DESC", (fleet["id"],))
    trips = db.q("SELECT t.*, v.code vcode, v.reg_label reg, v.driver_name driver FROM trips t JOIN vehicles v ON v.id=t.vehicle_id "
                 "WHERE v.fleet_id=? ORDER BY t.id DESC LIMIT 30", (fleet["id"],))
    return {"fleet": {k: fleet[k] for k in ("code", "name", "owner_name", "diesel_price")} | {"kind": fleet.get("kind") or "demo"},
            "orders": [{"id": l["id"], "route": f"{l['origin']} → {l['destination']}", "weight_t": l["weight_t"],
                        "freight": l["freight"], "party": (l.get("need") or "").removeprefix("party:") if (l.get("need") or "").startswith("party:") else "",
                        "ready_on": l["ready_on"], "urgent": bool(l.get("urgent"))} for l in loads],
            "trips": [{"id": t["id"], "vehicle": t["vcode"], "reg": t["reg"], "driver": t["driver"],
                       "route": f"{t['origin']} → {t['destination']}", "freight": t["freight_amount"], "client": t["client_name"],
                       "status": "delivered" if t.get("delivered_at") else t["status"], "started": t["started_at"],
                       "km": round(t.get("gps_km") or 0)} for t in trips]}
