"""CEO Agent: one per fleet, sits in the office.

1. Receives events from every Driver Agent and decides (by RULES) whether to log them for the
   daily digest, alert the owner now, or hand over to the owner immediately.
2. Answers the owner's questions (voice or text) with tools: fleet status, expenses, trip P&L,
   freight quotes, document expiry, tracking links, messaging a driver.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from .. import costing, db, geo
from ..bus import Out, bus, to_driver
from ..config import settings
from ..llm import Memory, Tool, params, run_agent
from .common import CATEGORY_HI, inr, mask_reg, today_ist_start_utc

log = logging.getLogger("saarthi.ceo")
memory = Memory()
BIG_EXPENSE = 10000  # single expense above this is reported to the owner immediately


def owner_out(fleet: dict, text: str, speak: str | None = None, **kw) -> Out | None:
    if not fleet or not fleet.get("owner_chat_id"):
        log.warning("fleet %s has no owner registered; message dropped: %s", fleet and fleet.get("code"), text)
        return None
    channel = "owner" if settings.owner_bot_token else "driver"
    return Out(chat_id=fleet["owner_chat_id"], text=text, speak=speak, channel=channel, **kw)


# =============== events from Driver Agents ===============
async def on_event(vehicle_id: int, kind: str, data: dict) -> str:
    """Route an event. Returns the routing decision (for logging/tests)."""
    v = db.vehicle(vehicle_id)
    f = db.fleet(v["fleet_id"])
    db.log_agent(f"driver:{v['code']}", f"ceo:{f['code']}", kind, data)
    who = f"{v['driver_name'] or 'Driver'} ({v['reg_label']})"
    decision = "digest"
    out = None

    if kind == "emergency":
        decision = "handover_owner"
        loc = data.get("maps") or "location not shared"
        out = owner_out(
            f, f"🚨 EMERGENCY — {who}\n{data.get('description','')}\nLocation: {loc}\n"
               f"Driver has been shown 112/108. Please call the driver NOW.",
            speak=f"Emergency. {v['driver_name'] or 'Driver'} ki gaadi mein emergency hai. Turant driver ko call kariye.",
            buttons=[("📞 Open driver chat (call)", f"tg://user?id={v['driver_chat_id']}")] if v.get("driver_chat_id") else [],
            location=data.get("latlon"),
        )
    elif kind == "breakdown":
        decision = "alert_owner"
        helpers = data.get("help", [])
        help_txt = "\n".join(f"• {h['name']} — {h['distance_km']} km" + (f" — {h['phone']}" if h.get("phone") else "")
                             for h in helpers[:3]) or "• none found nearby (searching wider / peer help)"
        out = owner_out(
            f, f"⚠️ Breakdown — {who}\nProblem: {data.get('description','')}\n"
               f"Location: {data.get('maps') or 'not shared'}\nNearest help sent to driver:\n{help_txt}\n"
               f"I am tracking it. Reply here to instruct me.",
            speak=f"{v['driver_name'] or 'Driver'} ki gaadi kharab hui hai: {data.get('description','')}. "
                  f"Maine nazdeeki mistri driver ko bhej diya hai.",
            location=data.get("latlon"),
        )
    elif kind == "fatigue_missed":
        decision = "alert_owner"
        out = owner_out(
            f, f"😴 {who} has not answered {data.get('missed', 2)} alertness check-ins while driving. "
               f"Please call the driver.",
            speak=f"{v['driver_name'] or 'Driver'} jawab nahi de rahe. Ek baar call kar lijiye.",
            buttons=[("📞 Open driver chat (call)", f"tg://user?id={v['driver_chat_id']}")] if v.get("driver_chat_id") else [],
        )
    elif kind in ("expense_confirmed", "income_confirmed") and data.get("amount", 0) >= BIG_EXPENSE:
        decision = "alert_owner"
        out = owner_out(f, f"💸 Large entry — {who}: {CATEGORY_HI.get(data['category'], data['category'])} "
                           f"{inr(data['amount'])}" + (f" ({data['note']})" if data.get("note") else ""))
    elif kind == "peer_help_matched":
        decision = "inform_owner"
        out = owner_out(f, f"🤝 Fleet Mesh: {data.get('text','')}")
    elif kind == "trip_started":
        decision = "inform_owner"
        out = owner_out(f, f"🚚 Trip started — {who}: {data.get('origin')} → {data.get('destination')}"
                           + (f", freight {inr(data['freight'])}" if data.get("freight") else ""))

    if out:
        await bus.send(out)
    db.log_agent(f"ceo:{f['code']}", "owner", decision, {"event": kind})
    return decision


# =============== tools for owner conversations ===============
def _fleet_tools(fleet_id: int) -> list[Tool]:
    def vehicles():
        return db.q("SELECT * FROM vehicles WHERE fleet_id=? ORDER BY code", (fleet_id,))

    def find_vehicle(code: str) -> dict:
        code = (code or "").strip().upper()
        for v in vehicles():
            if code in (v["code"].upper(), v["reg_label"].upper()) or code in v["reg_label"].upper() \
                    or (v["driver_name"] and code.lower() in v["driver_name"].lower()):
                return v
        raise ValueError(f"No vehicle matching '{code}'. Vehicles: {[x['code'] for x in vehicles()]}")

    async def fleet_status():
        rows = []
        for v in vehicles():
            t = db.active_trip(v["id"])
            inc = db.one("SELECT kind,description,severity FROM incidents WHERE vehicle_id=? AND status='open' "
                         "ORDER BY id DESC LIMIT 1", (v["id"],))
            rows.append({
                "vehicle": v["code"], "reg": v["reg_label"], "driver": v["driver_name"],
                "trip": f"{t['origin']} → {t['destination']}" if t else "no active trip",
                "last_location": geo.maps_link(v["last_lat"], v["last_lon"]) if v["last_lat"] else "unknown",
                "last_seen_utc": v["last_seen"], "speed_kmph": v["last_speed_kmph"],
                "companion": "live" if v["id"] in bus.live else "offline",
                "open_incident": inc,
            })
        return rows

    async def expenses_report(period: str = "today", vehicle_code: str | None = None):
        where, args = ["v.fleet_id=?", "e.status='confirmed'"], [fleet_id]
        if period == "today":
            where.append("e.created_at>=?"); args.append(today_ist_start_utc())
        elif period == "week":
            where.append("e.created_at>=?")
            args.append((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds"))
        if vehicle_code:
            where.append("v.id=?"); args.append(find_vehicle(vehicle_code)["id"])
        rows = db.q("SELECT v.code, e.category, SUM(e.amount) total, SUM(e.litres) litres, COUNT(*) n "
                    "FROM expenses e JOIN vehicles v ON v.id=e.vehicle_id WHERE " + " AND ".join(where) +
                    " GROUP BY v.code, e.category ORDER BY v.code", tuple(args))
        spend = sum(r["total"] for r in rows if not r["category"].startswith("freight_"))
        received = sum(r["total"] for r in rows if r["category"].startswith("freight_"))
        return {"period": period, "rows": rows, "total_spend": spend, "total_freight_received": received,
                "total_spend_inr": inr(spend)}

    async def trip_profit(vehicle_code: str):
        v = find_vehicle(vehicle_code)
        t = db.active_trip(v["id"]) or db.one("SELECT * FROM trips WHERE vehicle_id=? ORDER BY id DESC LIMIT 1", (v["id"],))
        if not t:
            return {"error": "no trip found"}
        rows = db.q("SELECT category, SUM(amount) s FROM expenses WHERE trip_id=? AND status='confirmed' "
                    "AND category NOT LIKE 'freight_%' GROUP BY category", (t["id"],))
        res = costing.trip_pnl(t["freight_amount"] or 0, {r["category"]: r["s"] for r in rows})
        res["trip"] = f"{t['origin']} → {t['destination']}"
        if not t["freight_amount"]:
            res["note"] = "Freight amount not set for this trip; profit is incomplete."
        return res

    async def quote_freight(origin: str, destination: str, vtype: str | None = None,
                            load_tonnes: float | None = None, offered_freight: float | None = None,
                            actual_tolls: float | None = None, empty_return_fraction: float = 0.0,
                            diesel_price: float | None = None, target_margin_pct: float = 15.0,
                            distance_km: float | None = None, broker_commission_pct: float = 0.0):
        f = db.fleet(fleet_id)
        vs = vehicles()
        default_vtype = vs[0]["vtype"] if vs else "truck_10w"
        route_info = None
        if distance_km is None:
            o, d = await geo.geocode(origin), await geo.geocode(destination)
            if not o or not d:
                return {"error": f"Could not find {'origin' if not o else 'destination'} on the map"}
            route_info = await geo.route(o["lat"], o["lon"], d["lat"], d["lon"])
            distance_km = route_info["routes"][0]["distance_km"]
        res = costing.estimate(costing.CostInputs(
            distance_km=distance_km, vtype=vtype or default_vtype,
            diesel_price=diesel_price or f["diesel_price"], load_tonnes=load_tonnes,
            actual_tolls=actual_tolls, empty_return_fraction=empty_return_fraction,
            target_margin_pct=target_margin_pct, offered_freight=offered_freight,
            broker_commission_pct=broker_commission_pct,
        ))
        res["route"] = f"{origin} → {destination}"
        if route_info:
            res["route_source"] = route_info["source"]
        return res

    async def documents_expiring(days: int = 30):
        limit = (date.today() + timedelta(days=days)).isoformat()
        return db.q("SELECT v.code, v.reg_label, d.doc_type, d.expiry_date FROM documents d "
                    "JOIN vehicles v ON v.id=d.vehicle_id WHERE v.fleet_id=? AND d.expiry_date<=? "
                    "ORDER BY d.expiry_date", (fleet_id, limit))

    async def open_incidents():
        return db.q("SELECT v.code, i.kind, i.severity, i.description, i.created_at FROM incidents i "
                    "JOIN vehicles v ON v.id=i.vehicle_id WHERE v.fleet_id=? AND i.status='open'", (fleet_id,))

    async def share_tracking_link(vehicle_code: str):
        v = find_vehicle(vehicle_code)
        t = db.active_trip(v["id"])
        if not t:
            return {"error": "no active trip; start a trip first"}
        return {"link": f"{settings.public_base_url}/track/{t['track_token']}",
                "note": "Share this with the client; it shows only this truck's live position for this trip."}

    async def message_driver(vehicle_code: str, message: str):
        v = find_vehicle(vehicle_code)
        if not v.get("driver_chat_id") and v["id"] not in bus.live:
            return {"error": "driver's companion is offline"}
        await bus.send(to_driver(v, f"🏢 Office: {message}", speak=f"Boss, office se sandesh: {message}"))
        db.log_agent(f"ceo:{fleet_id}", f"driver:{v['code']}", "instruction", {"message": message})
        return {"sent": True}

    async def resolve_incident(vehicle_code: str):
        v = find_vehicle(vehicle_code)
        db.ex("UPDATE incidents SET status='resolved', resolved_at=? WHERE vehicle_id=? AND status='open'",
              (db.now(), v["id"]))
        from . import mesh
        await mesh.close_requests(v, reason="solved")
        return {"resolved": True}

    async def set_diesel_price(price: float):
        if not 50 <= price <= 200:
            raise ValueError("diesel price looks wrong; expected ₹50–200 per litre")
        db.ex("UPDATE fleets SET diesel_price=? WHERE id=?", (price, fleet_id))
        return {"diesel_price": price}

    s = {"type": "string"}
    n = {"type": "number"}
    return [
        Tool("fleet_status", "Where every truck is, its trip, last update and any open incident.", params({}), fleet_status),
        Tool("expenses_report", "Confirmed expenses and freight received, grouped by truck and category.",
             params({"period": {"type": "string", "enum": ["today", "week", "all"]}, "vehicle_code": s}), expenses_report),
        Tool("trip_profit", "Profit/loss of a truck's current (or last) trip from logged expenses and freight.",
             params({"vehicle_code": s}, ["vehicle_code"]), trip_profit),
        Tool("quote_freight", "Exact cost breakdown and recommended quote for a new load (diesel + tolls + driver + "
             "maintenance + misc). Use offered_freight to check profit on a client's offer. vtype one of "
             f"{list(costing.PROFILES)}.",
             params({"origin": s, "destination": s, "vtype": s, "load_tonnes": n, "offered_freight": n,
                     "actual_tolls": n, "empty_return_fraction": n, "diesel_price": n, "target_margin_pct": n,
                     "distance_km": n, "broker_commission_pct": n}, ["origin", "destination"]), quote_freight),
        Tool("documents_expiring", "Vehicle documents (insurance, permit, fitness, PUC, licence) expiring within N days.",
             params({"days": {"type": "integer"}}), documents_expiring),
        Tool("open_incidents", "Unresolved breakdowns/emergencies.", params({}), open_incidents),
        Tool("resolve_incident", "Mark a truck's open incidents as resolved.", params({"vehicle_code": s}, ["vehicle_code"]), resolve_incident),
        Tool("share_tracking_link", "Client tracking link for a truck's active trip (no GPS hardware needed).",
             params({"vehicle_code": s}, ["vehicle_code"]), share_tracking_link),
        Tool("message_driver", "Send a spoken instruction to a driver.", params({"vehicle_code": s, "message": s},
             ["vehicle_code", "message"]), message_driver),
        Tool("set_diesel_price", "Update the fleet's diesel price per litre used in costing.",
             params({"price": n}, ["price"]), set_diesel_price),
    ]


def system_prompt(fleet: dict) -> str:
    vs = db.q("SELECT code, reg_label, driver_name, vtype FROM vehicles WHERE fleet_id=?", (fleet["id"],))
    return f"""You are the CEO Agent (office manager) of the transport company "{fleet['name']}".
You talk to the owner{(' ' + fleet['owner_name']) if fleet.get('owner_name') else ''} in the language they use
(Hindi, Hinglish or English). Be brief, precise and business-like. Today's diesel price: ₹{fleet['diesel_price']}/L.
Trucks: {vs}
Rules:
- ALWAYS use tools for facts and numbers. Never invent money figures, locations or statuses.
- For any freight quote or profit question call quote_freight / trip_profit and report the tool's numbers exactly,
  with the cost breakdown, and mention any warnings (estimated tolls, overloading, loss-making offer).
- If a question needs data you do not have, say so plainly.
- Format money in Indian style (₹1,25,000)."""


async def handle_owner(fleet: dict, text: str) -> str:
    key = f"owner:{fleet['id']}"
    reply = await run_agent(system_prompt(fleet), memory.get(key), text, _fleet_tools(fleet["id"]),
                            agent_name=f"ceo:{fleet['code']}")
    memory.add(key, "user", text)
    memory.add(key, "assistant", reply)
    return reply


async def daily_digest(fleet: dict) -> str:
    since = today_ist_start_utc()
    lines = [f"📊 Daily report — {fleet['name']}"]
    total = 0.0
    for v in db.q("SELECT * FROM vehicles WHERE fleet_id=? ORDER BY code", (fleet["id"],)):
        rows = db.q("SELECT category, SUM(amount) s FROM expenses WHERE vehicle_id=? AND status='confirmed' "
                    "AND created_at>=? GROUP BY category", (v["id"], since))
        spend = sum(r["s"] for r in rows if not r["category"].startswith("freight_"))
        got = sum(r["s"] for r in rows if r["category"].startswith("freight_"))
        total += spend
        t = db.active_trip(v["id"])
        trip = f"{t['origin']}→{t['destination']}" if t else "idle"
        detail = ", ".join(f"{CATEGORY_HI.get(r['category'], r['category'])} {inr(r['s'])}" for r in rows) or "no entries"
        lines.append(f"• {mask_reg(v['reg_label'])} ({v['driver_name'] or '-'}) {trip}: spend {inr(spend)}"
                     + (f", received {inr(got)}" if got else "") + f" — {detail}")
    inc = db.q("SELECT COUNT(*) n FROM incidents i JOIN vehicles v ON v.id=i.vehicle_id "
               "WHERE v.fleet_id=? AND i.created_at>=?", (fleet["id"], since))[0]["n"]
    docs = db.q("SELECT v.reg_label, d.doc_type, d.expiry_date FROM documents d JOIN vehicles v ON v.id=d.vehicle_id "
                "WHERE v.fleet_id=? AND d.expiry_date<=?", (fleet["id"], (date.today() + timedelta(days=30)).isoformat()))
    lines.append(f"Total spend today: {inr(total)} · Incidents: {inc}")
    for d in docs:
        overdue = d["expiry_date"] < date.today().isoformat()
        lines.append(f"{'❌ EXPIRED' if overdue else '⏰ Expiring'}: {mask_reg(d['reg_label'])} {d['doc_type']} {d['expiry_date']}")
    return "\n".join(lines)
