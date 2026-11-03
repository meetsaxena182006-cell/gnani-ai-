"""Driver Agent: one per truck. The driver talks (voice note -> Prisma); the agent acts with
tools and answers by voice (Timbre). Money is ALWAYS read back and confirmed before saving."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from .. import db, geo
from ..bus import Out, bus
from ..llm import Memory, Tool, params, run_agent
from ..safety import EMERGENCY_NUMBERS, final_severity, is_moving, yes_no
from . import ceo, mesh
from .common import CATEGORY_HI, EXPENSE_CATEGORIES, INCOME_CATEGORIES, inr

log = logging.getLogger("saarthi.driver")
memory = Memory()
PENDING_TTL_MIN = 15


# ---------------- confirmation flow (deterministic) ----------------
def pending_entries(vid: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=PENDING_TTL_MIN)).isoformat(timespec="seconds")
    db.ex("UPDATE expenses SET status='expired' WHERE vehicle_id=? AND status='pending' AND created_at<?", (vid, cutoff))
    return db.q("SELECT * FROM expenses WHERE vehicle_id=? AND status='pending' ORDER BY id", (vid,))


def describe(e: dict) -> str:
    s = f"{CATEGORY_HI.get(e['category'], e['category'])} {inr(e['amount'])}"
    if e.get("litres"):
        s += f", {e['litres']:g} litre"
    if e.get("note"):
        s += f" ({e['note']})"
    return s


def confirm_prompt(entries: list[dict]) -> str:
    items = "; ".join(describe(e) for e in entries)
    return f"{items} — sahi hai? Haan ya nahi boliye."


async def confirm_all(v: dict) -> str:
    entries = pending_entries(v["id"])
    for e in entries:
        db.ex("UPDATE expenses SET status='confirmed' WHERE id=?", (e["id"],))
        kind = "income_confirmed" if e["category"] in INCOME_CATEGORIES else "expense_confirmed"
        await ceo.on_event(v["id"], kind, {"category": e["category"], "amount": e["amount"], "note": e.get("note")})
    t = db.active_trip(v["id"])
    total = None
    if t:
        total = db.one("SELECT SUM(amount) s FROM expenses WHERE trip_id=? AND status='confirmed' "
                       "AND category NOT LIKE 'freight_%'", (t["id"],))["s"]
    msg = "Save ho gaya: " + "; ".join(describe(e) for e in entries) + "."
    if total:
        msg += f" Is trip ka kul kharcha {inr(total)}."
    return msg


def cancel_all(v: dict) -> None:
    db.ex("UPDATE expenses SET status='cancelled' WHERE vehicle_id=? AND status='pending'", (v["id"],))


# ---------------- tools ----------------
class Turn:
    def __init__(self) -> None:
        self.new_pending = False
        self.final_override: str | None = None


def make_tools(vid: int, turn: Turn, transcript: str) -> list[Tool]:
    def v() -> dict:
        return db.vehicle(vid)

    def loc() -> tuple[float, float] | None:
        x = v()
        return (x["last_lat"], x["last_lon"]) if x["last_lat"] is not None else None

    async def log_entry(category: str, amount: float, litres: float | None = None, note: str | None = None):
        if category not in EXPENSE_CATEGORIES + INCOME_CATEGORIES:
            raise ValueError(f"category must be one of {EXPENSE_CATEGORIES + INCOME_CATEGORIES}")
        if amount is None or amount <= 0 or amount > 500000:
            raise ValueError("amount must be between 1 and 5,00,000 rupees; ask the driver again")
        t = db.active_trip(vid)
        l = loc()
        db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,litres,note,lat,lon,status,transcript,created_at)"
              " VALUES(?,?,?,?,?,?,?,?, 'pending', ?, ?)",
              (vid, t["id"] if t else None, category, float(amount), litres, note,
               l[0] if l else None, l[1] if l else None, transcript, db.now()))
        turn.new_pending = True
        return {"status": "pending_confirmation", "will_be_read_back": True}

    async def start_trip(origin: str, destination: str, freight_amount: float | None = None, client: str | None = None):
        t = db.start_trip(vid, origin, destination, freight_amount, client)
        await ceo.on_event(vid, "trip_started", {"origin": origin, "destination": destination, "freight": freight_amount})
        return {"trip_id": t["id"], "origin": origin, "destination": destination}

    async def end_trip():
        t = db.active_trip(vid)
        if not t:
            return {"error": "no active trip"}
        db.ex("UPDATE trips SET status='closed', ended_at=? WHERE id=?", (db.now(), t["id"]))
        return {"closed": True}

    async def trip_summary():
        t = db.active_trip(vid)
        if not t:
            return {"error": "no active trip"}
        rows = db.q("SELECT category, SUM(amount) s, SUM(litres) l FROM expenses WHERE trip_id=? AND status='confirmed' "
                    "GROUP BY category", (t["id"],))
        spend = sum(r["s"] for r in rows if not r["category"].startswith("freight_"))
        return {"trip": f"{t['origin']} → {t['destination']}", "by_category": rows, "total_spend": inr(spend),
                "freight": inr(t["freight_amount"])}

    async def report_breakdown(problem: str, severity: str = "high"):
        x = v()
        sev = final_severity(severity, problem + " " + transcript)
        l = loc()
        iid = db.ex("INSERT INTO incidents(vehicle_id,kind,severity,description,lat,lon,created_at) VALUES(?,?,?,?,?,?,?)",
                    (vid, "emergency" if sev == "critical" else "breakdown", sev, problem,
                     l[0] if l else None, l[1] if l else None, db.now()))
        maps = geo.maps_link(*l) if l else None
        if sev == "critical":
            await bus.send(Out(chat_id=x["driver_chat_id"],
                               text="🚨 Turant 112 pe call karein (police/ambulance). Neeche number dabaiye.",
                               contacts=EMERGENCY_NUMBERS[:2]))
            await ceo.on_event(vid, "emergency", {"description": problem, "maps": maps, "latlon": l, "incident": iid})
            turn.final_override = "Abhi 112 pe call kariye. Maine malik ko turant bata diya hai. Aap surakshit jagah par rahiye."
            return {"severity": "critical", "action": "112 shown to driver, owner alerted"}
        help_list: list[dict] = []
        if l:
            kind = "tyre" if re.search(r"tyre|tair|puncture|pancture|phat|burst", (problem + transcript).lower()) else "mechanic"
            try:
                help_list = await geo.nearby(kind, l[0], l[1])
            except Exception as e:
                log.warning("nearby search failed: %s", e)
        await ceo.on_event(vid, "breakdown", {"description": problem, "maps": maps, "latlon": l, "help": help_list})
        if help_list:
            with_phone = [(h["name"], h["phone"]) for h in help_list if h.get("phone")][:3]
            lines = "\n".join(f"• {h['name']} — {h['distance_km']} km — {h['maps']}" for h in help_list[:3])
            await bus.send(Out(chat_id=x["driver_chat_id"], text=f"🔧 Nazdeeki madad:\n{lines}", contacts=with_phone,
                               location=(help_list[0]["lat"], help_list[0]["lon"])))
        peer = None
        if l and not any(h.get("phone") for h in help_list):
            peer = await mesh.request_help(x, problem)
        return {"severity": sev, "owner_alerted": True, "location_known": bool(l),
                "nearest_help": [{"name": h["name"], "km": h["distance_km"], "has_phone": bool(h.get("phone"))} for h in help_list[:3]],
                "peer_help": peer,
                "instruction": None if l else "Ask the driver to share live location (📎 → Location)."}

    async def find_help(kind: str):
        l = loc()
        if not l:
            return {"error": "no location; ask driver to share live location"}
        res = await geo.nearby(kind, l[0], l[1])
        if res:
            await bus.send(Out(chat_id=v()["driver_chat_id"],
                               text="\n".join(f"• {h['name']} — {h['distance_km']} km — {h['maps']}" for h in res[:3]),
                               contacts=[(h["name"], h["phone"]) for h in res if h.get("phone")][:3]))
        return {"results": [{"name": h["name"], "km": h["distance_km"]} for h in res[:3]]}

    async def request_peer_help(need: str):
        return await mesh.request_help(v(), need)

    async def weather_now():
        l = loc()
        if not l:
            return {"error": "no location"}
        return await geo.weather(*l)

    async def route_ahead():
        l, t = loc(), db.active_trip(vid)
        if not l or not t:
            return {"error": "need live location and an active trip with destination"}
        d = await geo.geocode(t["destination"])
        if not d:
            return {"error": "destination not found on map"}
        r = await geo.route(l[0], l[1], d["lat"], d["lon"])
        warnings = []
        for (lat, lon) in (r["routes"][0].get("sample_points") or [])[1:]:
            try:
                w = await geo.weather(lat, lon)
                warnings += [f"{x} (~ahead)" for x in w["warnings"]]
            except Exception:
                pass
        r["weather_warnings_on_route"] = sorted(set(warnings))
        for rt in r["routes"]:
            rt.pop("sample_points", None)
        return r

    async def my_documents():
        return db.q("SELECT doc_type, expiry_date, CASE WHEN expiry_date < ? THEN 'EXPIRED' ELSE 'valid' END status "
                    "FROM documents WHERE vehicle_id=? ORDER BY expiry_date", (date.today().isoformat(), vid))

    async def message_office(message: str):
        x = v()
        f = db.fleet(x["fleet_id"])
        db.log_agent(f"driver:{x['code']}", f"ceo:{f['code']}", "driver_message", {"message": message})
        o = ceo.owner_out(f, f"📩 {x['driver_name'] or 'Driver'} ({x['reg_label']}): {message}")
        if o:
            await bus.send(o)
        return {"sent": True}

    s, n = {"type": "string"}, {"type": "number"}
    return [
        Tool("log_entry", "Record ONE money entry the driver mentions (expense or freight received). It is saved "
             "only after the driver confirms. Call once per amount. Amount in rupees as a number.",
             params({"category": {"type": "string", "enum": EXPENSE_CATEGORIES + INCOME_CATEGORIES},
                     "amount": n, "litres": n, "note": s}, ["category", "amount"]), log_entry),
        Tool("start_trip", "Start a new trip (origin, destination, optional total freight and client).",
             params({"origin": s, "destination": s, "freight_amount": n, "client": s}, ["origin", "destination"]), start_trip),
        Tool("end_trip", "Close the current trip (delivered).", params({}), end_trip),
        Tool("trip_summary", "Expenses so far on this trip.", params({}), trip_summary),
        Tool("report_breakdown", "Driver reports a breakdown, puncture, accident, injury, theft or any emergency. "
             "severity: low (minor), high (vehicle stopped), critical (accident/injury/fire/robbery).",
             params({"problem": s, "severity": {"type": "string", "enum": ["low", "high", "critical"]}}, ["problem"]),
             report_breakdown),
        Tool("find_help", "Find nearest mechanic/tyre shop/fuel/hospital/police/dhaba.",
             params({"kind": {"type": "string", "enum": ["mechanic", "tyre", "fuel", "hospital", "police", "dhaba"]}}, ["kind"]),
             find_help),
        Tool("request_peer_help", "Ask nearby Saarthi trucks (other drivers) for help.", params({"need": s}, ["need"]),
             request_peer_help),
        Tool("weather_now", "Weather and warnings at the truck's location.", params({}), weather_now),
        Tool("route_ahead", "Route options to the destination, traffic (if available) and weather warnings ahead.",
             params({}), route_ahead),
        Tool("my_documents", "This truck's document expiry dates.", params({}), my_documents),
        Tool("message_office", "Pass a message to the office/owner.", params({"message": s}, ["message"]), message_office),
    ]


def system_prompt(v: dict, moving: bool) -> str:
    f = db.fleet(v["fleet_id"])
    t = db.active_trip(v["id"])
    trip = f"{t['origin']} → {t['destination']}" if t else "none"
    mode = ("THE TRUCK IS MOVING: reply in at most 12 words, ask only yes/no questions, never long explanations. "
            "Safety first.") if moving else "Truck is parked/slow: you may give fuller answers (still short, 1-3 sentences)."
    return f"""You are "Saarthi", the voice co-driver agent for driver {v['driver_name'] or ''} of truck {v['reg_label']}
(fleet "{f['name']}"). Current trip: {trip}. Location known: {v['last_lat'] is not None}.
The driver speaks Hindi/Hinglish/Rajasthani; transcripts come from speech recognition and may contain errors.
Reply in simple spoken Hinglish written in Latin script, warm and respectful ("aap"). Replies are converted to voice.
{mode}
Rules:
- Any money the driver mentions (diesel, toll, khana, repair, challan, bhaada/freight received...) -> call log_entry
  once per amount. Never claim something is saved; the system reads it back for confirmation.
- If the amount is unclear, ask again instead of guessing.
- Breakdown/puncture/accident/injury/fire/loot -> call report_breakdown immediately.
- For injury or accident, tell the driver to call 112. You never call emergency services yourself.
- Use tools for weather, route, documents, trip summary. Never invent facts.
- If you do not understand, ask the driver to repeat."""


async def handle(v: dict, text: str) -> str:
    """Process one driver utterance; sends the reply via the bus and returns it."""
    text = (text or "").strip()
    if not text:
        reply = "Awaaz saaf nahi aayi, dobara boliye."
        await bus.send(Out(chat_id=v["driver_chat_id"], text=reply, speak=reply))
        return reply

    pend = pending_entries(v["id"])
    if pend:
        yn = yes_no(text)
        if yn is True:
            reply = await confirm_all(v)
            await _reply(v, text, reply)
            return reply
        if yn is False and not re.search(r"\d", text):
            cancel_all(v)
            reply = "Theek hai, cancel kar diya. Sahi amount dobara boliye."
            await _reply(v, text, reply)
            return reply
        cancel_all(v)  # driver said something else (e.g. a correction); start over cleanly

    # fatigue check-in acknowledgement
    from ..fatigue import ack_if_pending
    if yes_no(text) is True and ack_if_pending(v["id"]):
        reply = "Bahut badhiya. Dhyan se chalaiye, paani peete rahiye."
        await _reply(v, text, reply)
        return reply

    turn = Turn()
    moving = is_moving(v)
    reply = await run_agent(system_prompt(v, moving), memory.get(f"drv:{v['id']}"), text,
                            make_tools(v["id"], turn, text), agent_name=f"driver:{v['code']}")
    if turn.final_override:
        reply = turn.final_override
    elif turn.new_pending:
        reply = confirm_prompt(pending_entries(v["id"]))
    await _reply(v, text, reply)
    return reply


async def _reply(v: dict, user_text: str, reply: str) -> None:
    memory.add(f"drv:{v['id']}", "user", user_text)
    memory.add(f"drv:{v['id']}", "assistant", reply)
    await bus.send(Out(chat_id=v["driver_chat_id"], text=reply, speak=reply))
