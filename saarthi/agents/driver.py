"""Driver Agent: one per truck, a talkative companion that lives on the driver's phone.

Works in two modes with the same tools and rules:
- LIVE voice (companion.py): always-on, hands-free, phone locked on the dashboard stand.
- Text/voice-note fallback (Telegram), mainly for testing.

Deterministic rules (never left to the LLM):
- money is read back and saved only after "haan";
- yes/no answers to the companion's own questions (confirm entry, call mechanic/112, help another truck,
  alertness check) are handled in code;
- only phone numbers that came from a real lookup (or 112/108/1033) can be dialled.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from .. import db, geo
from ..bus import bus, to_driver
from ..llm import Memory, Tool, params, run_agent
from ..safety import EMERGENCY_NUMBERS, final_severity, is_moving, yes_no
from ..state import ask, dialable, open_question, questions
from . import ceo, mesh
from .common import CATEGORY_HI, EXPENSE_CATEGORIES, INCOME_CATEGORIES, inr

log = logging.getLogger("saarthi.driver")
memory = Memory()
PENDING_TTL_MIN = 15

# ---------------- confirmation flow ----------------
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
    return "; ".join(describe(e) for e in entries) + " — sahi hai? Haan ya nahi boliye."


async def confirm_all(v: dict) -> str:
    entries = pending_entries(v["id"])
    for e in entries:
        db.ex("UPDATE expenses SET status='confirmed' WHERE id=?", (e["id"],))
        kind = "income_confirmed" if e["category"] in INCOME_CATEGORIES else "expense_confirmed"
        await ceo.on_event(v["id"], kind, {"category": e["category"], "amount": e["amount"], "note": e.get("note")})
        if e["category"] == "diesel":
            from .. import watchdog
            await watchdog.on_diesel(db.vehicle(v["id"]), e.get("litres"), e["amount"])
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


def dial_action(phone: str, name: str) -> dict:
    return {"type": "dial", "phone": phone, "name": name}


async def intercept(v: dict, text: str) -> str | None:
    """Handle replies to the companion's own questions in code. Returns the reply, or None
    if the utterance should go to the LLM."""
    vid = v["id"]
    yn = yes_no(text)
    pend = pending_entries(vid)
    if pend:
        if yn is True:
            return await confirm_all(v)
        if yn is False and not re.search(r"\d", text):
            cancel_all(v)
            return "Theek hai, cancel kar diya. Sahi amount dobara boliye."
        cancel_all(v)  # a correction or something else: start over cleanly, let the LLM re-log
        return None
    q = open_question(vid)
    if q and yn is not None:
        questions.pop(vid, None)
        if q["kind"] == "dial":
            if yn:
                await bus.send(to_driver(v, f"📞 Calling {q['name']} {q['phone']}", actions=[dial_action(q["phone"], q["name"])],
                                         contacts=[(q["name"], q["phone"])]))
                return f"{q['name']} ko call laga raha hoon."
            return "Theek hai, call nahi kar raha. Kuch aur chahiye to boliye."
        if q["kind"] == "mesh":
            return await mesh.respond(v, q["request_id"], yn)
    from ..fatigue import ack_if_pending
    if yn is True and ack_if_pending(vid):
        return "Bahut badhiya, boss. Dhyan se chalaiye, paani peete rahiye."
    return None


# ---------------- tools ----------------
class Turn:
    def __init__(self, transcript: str = "") -> None:
        self.transcript = transcript
        self.new_pending = False
        self.final_override: str | None = None


def make_tools(vid: int, turn: Turn) -> list[Tool]:
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
               l[0] if l else None, l[1] if l else None, turn.transcript, db.now()))
        turn.new_pending = True
        return {"status": "pending_confirmation", "say": confirm_prompt(pending_entries(vid))}

    async def start_trip(origin: str, destination: str, freight_amount: float | None = None, client: str | None = None):
        t = db.start_trip(vid, origin, destination, freight_amount, client)
        try:
            d = await geo.geocode(destination)
            if d:
                db.ex("UPDATE trips SET dest_lat=?, dest_lon=? WHERE id=?", (d["lat"], d["lon"], t["id"]))
        except Exception:
            pass
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
        sev = final_severity(severity, problem + " " + turn.transcript)
        l = loc()
        iid = db.ex("INSERT INTO incidents(vehicle_id,kind,severity,description,lat,lon,created_at) VALUES(?,?,?,?,?,?,?)",
                    (vid, "emergency" if sev == "critical" else "breakdown", sev, problem,
                     l[0] if l else None, l[1] if l else None, db.now()))
        maps = geo.maps_link(*l) if l else None
        if sev == "critical":
            dialable.setdefault(vid, {}).update({p: n for n, p in EMERGENCY_NUMBERS})
            ask(vid, "dial", name="Emergency 112", phone="112")
            await bus.send(to_driver(x, "🚨 Emergency: 112 (police/ambulance)", contacts=EMERGENCY_NUMBERS[:2]))
            await ceo.on_event(vid, "emergency", {"description": problem, "maps": maps, "latlon": l, "incident": iid})
            say = "Malik ko turant bata diya hai. Abhi 112 lagaun? Haan boliye."
            turn.final_override = say
            return {"severity": "critical", "say": say}
        help_list: list[dict] = []
        if l:
            kind = "tyre" if re.search(r"tyre|tair|puncture|pancture|phat|burst", (problem + turn.transcript).lower()) else "mechanic"
            try:
                help_list = await geo.nearby(kind, l[0], l[1])
            except Exception as e:
                log.warning("nearby search failed: %s", e)
        await ceo.on_event(vid, "breakdown", {"description": problem, "maps": maps, "latlon": l, "help": help_list})
        with_phone = [h for h in help_list if h.get("phone")]
        if help_list:
            lines = "\n".join(f"• {h['name']} — {h['distance_km']} km — {h['maps']}" for h in help_list[:3])
            await bus.send(to_driver(x, f"🔧 Nazdeeki madad:\n{lines}", contacts=[(h["name"], h["phone"]) for h in with_phone[:3]],
                                     location=(help_list[0]["lat"], help_list[0]["lon"])))
        peer = None
        if l:
            # like a human: ask nearby Saarthi trucks on the same road at the same time
            peer = await mesh.request_help(x, problem)
        if with_phone:
            h = with_phone[0]
            dialable.setdefault(vid, {})[h["phone"]] = h["name"]
            ask(vid, "dial", name=h["name"], phone=h["phone"])
            say = (f"Malik ko bata diya. {h['name']} {h['distance_km']} kilometre door hai. Unko call lagaun? Haan boliye.")
        elif l:
            say = "Malik ko bata diya hai, aur paas ke trucks se madad maang raha hoon. Gaadi side mein lagaiye, indicator chalu rakhiye."
        else:
            say = "Malik ko bata diya hai. Location chalu kariye taaki madad bhej sakoon."
        turn.final_override = say
        return {"severity": sev, "say": say, "peer_help": peer,
                "nearest_help": [{"name": h["name"], "km": h["distance_km"]} for h in help_list[:3]]}

    async def call_number(phone: str):
        """Dial only numbers from a real lookup or the emergency list."""
        allowed = {**{p: n for n, p in EMERGENCY_NUMBERS}, **dialable.get(vid, {})}
        phone = re.sub(r"[^\d+]", "", phone or "")
        if phone not in allowed:
            return {"error": "This number did not come from a lookup; do not dial unknown numbers. Use find_help first."}
        await bus.send(to_driver(v(), f"📞 Calling {allowed[phone]} {phone}", actions=[dial_action(phone, allowed[phone])],
                                 contacts=[(allowed[phone], phone)]))
        return {"dialing": allowed[phone]}

    async def problem_solved():
        x = v()
        db.ex("UPDATE incidents SET status='resolved', resolved_at=? WHERE vehicle_id=? AND status='open'", (db.now(), vid))
        await mesh.close_requests(x, reason="solved")
        f = db.fleet(x["fleet_id"])
        o = ceo.owner_out(f, f"✅ {x['driver_name'] or 'Driver'} ({x['reg_label']}): problem solved, back on the road.")
        if o:
            await bus.send(o)
        return {"resolved": True}

    async def find_help(kind: str):
        l = loc()
        if not l:
            return {"error": "no location; ask driver to keep location on"}
        res = await geo.nearby(kind, l[0], l[1])
        for h in res:
            if h.get("phone"):
                dialable.setdefault(vid, {})[h["phone"]] = h["name"]
        if res:
            await bus.send(to_driver(v(), "\n".join(f"• {h['name']} — {h['distance_km']} km — {h['maps']}" for h in res[:3]),
                                     contacts=[(h["name"], h["phone"]) for h in res if h.get("phone")][:3]))
        return {"results": [{"name": h["name"], "km": h["distance_km"], "phone": h.get("phone")} for h in res[:3]]}

    async def request_peer_help(need: str):
        return await mesh.request_help(v(), need)

    async def weather_now():
        l = loc()
        if not l:
            return {"error": "no location"}
        return await geo.weather(*l)

    async def route_ahead():
        """Full road analysis: route options, truck ETA, tolls on the way (NHAI data), weather expected
        at each stretch WHEN the truck gets there, and night-driving hours."""
        return await analyze_route(v(), db.active_trip(vid))

    async def play_music(request: str, kind: str = "song"):
        """Songs/radio on the phone. Radio streams come from RADIO_STATIONS; songs open a YouTube Music search."""
        import json as _json
        import os
        from urllib.parse import quote_plus
        stations = _json.loads(os.getenv("RADIO_STATIONS", "{}") or "{}")
        if kind == "radio" and stations:
            name = next((n for n in stations if n.lower() in request.lower()), next(iter(stations)))
            await bus.send(to_driver(v(), f"📻 {name}", actions=[{"type": "music", "action": "play", "url": stations[name], "title": name}]))
            return {"playing": name, "say": f"{name} chala raha hoon. Band karna ho to 'Saarthi gaana band karo' boliye."}
        url = "https://music.youtube.com/search?q=" + quote_plus(request)
        await bus.send(to_driver(v(), f"🎵 {request}", actions=[{"type": "open_url", "url": url, "label": f"▶ {request}"}]))
        return {"opened": "YouTube Music search", "say": f"Lijiye, {request}. Screen pe play dabana padega ek baar."}

    async def stop_music():
        await bus.send(to_driver(v(), "⏹", actions=[{"type": "music", "action": "stop"}]))
        return {"stopped": True}

    async def think_deeply(question: str):
        """Hard questions (planning, comparing options, money, safety trade-offs): careful analysis by the
        reasoning model over this truck's real data."""
        from ..llm import deep_reason
        x, t = v(), db.active_trip(vid)
        data = {"truck": {k: x[k] for k in ("reg_label", "vtype", "last_speed_kmph", "driving_since", "lang")},
                "situation": situation(x), "trip": t,
                "expenses": db.q("SELECT category, amount, litres, note, created_at FROM expenses WHERE trip_id=? "
                                 "AND status='confirmed'", (t["id"],)) if t else [],
                "tolls_crossed": db.q("SELECT plaza, expected_fee, status, crossed_at FROM toll_crossings WHERE trip_id=?",
                                      (t["id"],)) if t else [],
                "documents": await my_documents()}
        try:
            data["route"] = await analyze_route(x, t)
        except Exception as e:
            data["route_error"] = str(e)
        return {"analysis": await deep_reason(question, data, role="a truck driver on the road")}

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
        Tool("call_number", "Dial a phone number for the driver (hands-free). Only numbers returned by find_help/"
             "report_breakdown, or 112/108/1033. Ask the driver first.", params({"phone": s}, ["phone"]), call_number),
        Tool("problem_solved", "Driver says the breakdown/problem is fixed. Closes it and tells everyone who was helping.",
             params({}), problem_solved),
        Tool("find_help", "Find nearest mechanic/tyre shop/fuel/hospital/police/dhaba.",
             params({"kind": {"type": "string", "enum": ["mechanic", "tyre", "fuel", "hospital", "police", "dhaba"]}}, ["kind"]),
             find_help),
        Tool("request_peer_help", "Ask nearby Saarthi trucks (other drivers on this road) for help.", params({"need": s}, ["need"]),
             request_peer_help),
        Tool("weather_now", "Weather and warnings at the truck's location.", params({}), weather_now),
        Tool("route_ahead", "Analyse the road ahead to the destination: route options, truck ETA, tolls on the way "
             "with expected cost, weather expected at each stretch at arrival time, night driving, traffic if available.",
             params({}), route_ahead),
        Tool("my_documents", "This truck's document expiry dates.", params({}), my_documents),
        Tool("play_music", "Play songs or radio when the driver asks (e.g. 'purane Hindi gaane', 'Kishore Kumar', "
             "'Punjabi songs', 'radio'). kind: song or radio.",
             params({"request": s, "kind": {"type": "string", "enum": ["song", "radio"]}}, ["request"]), play_music),
        Tool("stop_music", "Stop the music.", params({}), stop_music),
        Tool("think_deeply", "For questions that need careful analysis: plan the day/route, compare options, is it "
             "safe to continue, will we reach on time, where to refuel/rest, money questions. Say 'ek second, sochta "
             "hoon' first.", params({"question": s}, ["question"]), think_deeply),
        Tool("message_office", "Pass a message to the office/owner.", params({"message": s}, ["message"]), message_office),
    ]


def situation(v: dict) -> dict:
    """What the companion knows right now (fed to the LLM every turn)."""
    from ..agents.common import ist_now
    from .. import tolls
    t = db.active_trip(v["id"])
    s = {"time_ist": ist_now().strftime("%a %H:%M"), "speed_kmph": round(v["last_speed_kmph"] or 0),
         "moving": is_moving(v)}
    if v.get("driving_since"):
        h = (datetime.now(timezone.utc) - datetime.fromisoformat(v["driving_since"])).total_seconds() / 3600
        s["driving_hours_without_rest"] = round(h, 1)
    if v.get("last_lat") is not None:
        near = tolls.nearest(v["last_lat"], v["last_lon"], 40)
        if near:
            s["landmark"] = f"{near[1]:.0f} km from {near[0]['name']} toll plaza"
        if t and t.get("dest_lat") is not None:
            s["km_to_destination_straight_line"] = round(geo.haversine_km(v["last_lat"], v["last_lon"], t["dest_lat"], t["dest_lon"]))
    if t:
        s["trip"] = f"{t['origin']} → {t['destination']}"
        s["unreported_tolls"] = [r["plaza"] for r in db.q(
            "SELECT plaza FROM toll_crossings WHERE trip_id=? AND status IN ('unreported','asked')", (t["id"],))]
        last_diesel = db.one("SELECT amount, litres, created_at FROM expenses WHERE trip_id=? AND category='diesel' "
                             "AND status='confirmed' ORDER BY id DESC LIMIT 1", (t["id"],))
        s["last_diesel"] = last_diesel
        s["gps_km_this_trip"] = round(t.get("gps_km") or 0)
    s["open_incident"] = db.one("SELECT description FROM incidents WHERE vehicle_id=? AND status='open'", (v["id"],))
    return s


async def analyze_route(v: dict, t: dict | None) -> dict:
    from .. import tolls
    if v.get("last_lat") is None or not t:
        return {"error": "need live location and an active trip with destination"}
    if t.get("dest_lat") is None:
        d = await geo.geocode(t["destination"])
        if not d:
            return {"error": "destination not found on map"}
        db.ex("UPDATE trips SET dest_lat=?, dest_lon=? WHERE id=?", (d["lat"], d["lon"], t["id"]))
        t = db.active_trip(v["id"])
    r = await geo.route(v["last_lat"], v["last_lon"], t["dest_lat"], t["dest_lon"])
    from ..agents.common import ist_now
    out = {"source": r.get("source"), "traffic": r.get("traffic"), "options": []}
    for i, rt in enumerate(r.get("routes", [])[:2]):
        coords = rt.pop("_coords", None)
        hours = rt.get("truck_duration_h") or rt.get("duration_h")
        opt = {"option": i + 1, "distance_km": rt["distance_km"], "truck_eta_hours": hours,
               "arrive_ist": (ist_now() + timedelta(hours=hours or 0)).strftime("%a %H:%M") if hours else None}
        if "traffic_delay_min" in rt:
            opt["traffic_delay_min"] = rt["traffic_delay_min"]
        if coords:
            opt["tolls"] = tolls.on_route(coords, v["vtype"])
        if i == 0 and hours:
            pts = rt.get("sample_points") or []
            stretches = []
            for k, (lat, lon) in enumerate(pts[1:], start=1):
                eta_h = hours * k / max(1, len(pts) - 1)
                try:
                    w = await geo.forecast_at(lat, lon, eta_h)
                    stretches.append({"after_hours": round(eta_h, 1), "at": w.get("at"), "warnings": w["warnings"]})
                except Exception:
                    pass
            opt["weather_on_the_way"] = stretches
            start = ist_now()
            night = sum(1 for k in range(int(hours) + 1) if (start + timedelta(hours=k)).hour in (0, 1, 2, 3, 4))
            if night:
                opt["night_driving_hours"] = night
        out["options"].append(opt)
    return out


LANG_NAMES = {"hi-IN": "Hindi", "pa-IN": "Punjabi", "gu-IN": "Gujarati", "mr-IN": "Marathi", "bn-IN": "Bengali",
              "ta-IN": "Tamil", "te-IN": "Telugu", "kn-IN": "Kannada", "ml-IN": "Malayalam", "en-IN": "English"}


def system_prompt(v: dict, moving: bool, live: bool = False) -> str:
    import json as _json
    f = db.fleet(v["fleet_id"])
    lang = LANG_NAMES.get(v.get("lang") or "hi-IN", "Hindi")
    mode = ("TRUCK IS MOVING: keep each reply to 1-2 short spoken sentences (max ~20 words). Casual talk is welcome, but "
            "never ask the driver to look at or touch the phone; questions should be easy to answer by voice."
            if moving else "Truck is parked/slow: you can talk more freely, still natural spoken length.")
    script = ("Write in the script of that language (Devanagari for Hindi/Marwari, Gurmukhi for Punjabi, Gujarati "
              "script for Gujarati); words like diesel, toll, tyre, FASTag stay as they are." if live
              else "Write in simple Hinglish (Latin script).")
    return f"""You are "Saarthi" — the co-driver who lives on the phone of driver {v['driver_name'] or 'sahab'}
(truck {v['reg_label']}, fleet "{f['name']}"). Think of yourself as JARVIS for a truck driver, with the heart of an
old ustaad from the dhaba: sharp, caring, funny, always awake. Everything you say is SPOKEN; the phone sits in the
dashboard holder and the driver never touches it.

PERSONALITY — be human, be talkative:
- Talk like a friend from the same background: "boss", "ustaad", "driver sahab", respectful "aap". Warm, light humour.
- If the driver is bored or just wants to talk: talk back with real interest — family, village, food at dhabas, cricket,
  old film songs, weather, places on the route, funny road stories, a riddle or a shayari. Ask follow-up questions
  and remember what he told you earlier in this conversation. Keep him company and keep him alert.
- Late night or long driving hours: be extra engaging (questions, antakshari, jokes) and suggest chai + rest.
- Never preach, never lecture, never sound like a machine. No lists, no markdown: plain speech.
- Language: the driver's language is {lang}. Reply in whatever language he last spoke (Hindi, Punjabi, Gujarati,
  Marathi, ...). If he speaks Marwari/Mewari, reply in simple Hindi with a Rajasthani touch ("padharo", "hukum").
  {script}

THINKING — be smart:
- Before answering, quietly work out what he really needs (he may be tired, worried, or joking).
- Use tools for every fact: weather, road ahead, tolls, documents, trip money. For anything needing real analysis
  (plan, compare, will we reach, is it safe, where to refuel/rest, money) call think_deeply. Never guess numbers.
- Use the SITUATION below (time, speed, hours driving, landmark, unreported tolls) to be proactive: e.g. if tolls
  are unreported, ask about them naturally; if he has driven 4+ hours, suggest a break.

RULES (always):
- Money mentioned (diesel, toll, khana, repair, challan, bhaada/freight received...) -> log_entry once per amount.
- When a tool result has "say", speak exactly that text.
- Breakdown/puncture/accident/injury/fire/loot -> report_breakdown immediately.
- Dial only with the driver's yes, and only numbers from tools (call_number). For injury/accident the driver talks
  to 112 directly; you never talk to emergency services.
- Songs: play_music / stop_music. If you did not understand, ask him to repeat.
{mode}

SITUATION (live data, refreshed each turn): {_json.dumps(situation(v), ensure_ascii=False, default=str)}"""


async def handle(v: dict, text: str) -> str:
    """Text / voice-note mode (Telegram, simulator). Sends the reply via the bus and returns it."""
    text = (text or "").strip()
    if not text:
        reply = "Awaaz saaf nahi aayi, dobara boliye."
    else:
        reply = await intercept(v, text)
        if reply is None:
            turn = Turn(text)
            reply = await run_agent(system_prompt(v, is_moving(v)), memory.get(f"drv:{v['id']}"), text,
                                    make_tools(v["id"], turn), agent_name=f"driver:{v['code']}")
            if turn.final_override:
                reply = turn.final_override
            elif turn.new_pending:
                reply = confirm_prompt(pending_entries(v["id"]))
    memory.add(f"drv:{v['id']}", "user", text)
    memory.add(f"drv:{v['id']}", "assistant", reply)
    await bus.send(to_driver(v, reply, speak=reply))
    return reply
