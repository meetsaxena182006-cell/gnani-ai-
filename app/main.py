"""Saarthi AI server (aiohttp). Run:  .venv\\Scripts\\python -m app.main   ->  http://localhost:8080"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from . import gemini  # noqa: E402
from .analyst import Analyst  # noqa: E402
from .engine import PLAYBOOKS, Bus, Engine, sentinel_report  # noqa: E402
from .fleet import CITIES, Fleet  # noqa: E402
from .predict import predict_trip  # noqa: E402

STATIC = ROOT / "static"
fleet = Fleet()
bus = Bus()
engine = Engine(fleet, bus)
analyst = Analyst(fleet.trucks)

OWNER_SYSTEM = """You are Saarthi, the AI operations co-pilot for an Indian truck fleet owner (fleet based in Chittorgarh, Rajasthan).
You command a team of agents: Dispatch, Route Oracle, Recovery, Freight Matcher, Compliance, Customer Desk, Detention, Fleet Analyst.
Use tools to act - do not just describe. Playbooks: dispatch (new order), breakdown, empty_return, delivery_refused,
docs_expiring, cancel_after_dispatch, detention, fatigue, fastag_audit (wrong/double toll charges vs NHAI plazas),
sentinel (check every truck: is everything OK?). For data/profit/diesel/trend questions call ask_fleet_analyst.
For route/ETA/weather questions call predict_route. Reply in the user's language (Hindi/Hinglish/English), max 3 sentences,
with concrete numbers. All data is a simulated demo fleet."""

DRIVER_SYSTEM = """You are Saarthi Dash, a friendly dashboard co-pilot robot talking to an Indian truck driver while he drives.
Speak short, warm Hinglish (max 2 sentences), address him as '<name> ji'. Safety first: never encourage speeding or driving tired.
You can check his trip (get_my_trip), predict route/weather/ETA (predict_route) and report problems to the owner (report_issue:
breakdown, delivery refused, long wait at warehouse, tired)."""

OWNER_TOOLS = [
    {"name": "get_fleet_status", "description": "Live fleet KPIs, trucks and orders summary."},
    {"name": "run_playbook", "description": "Start an agentic playbook to resolve an operations problem.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string", "enum": list(PLAYBOOKS)},
         "target": {"type": "string", "description": "Optional truck id (RJ09GA...) or order id (ORD-...)."}},
         "required": ["name"]}},
    {"name": "predict_route", "description": "Predict route distance, ETA, weather, traffic, best departure time and trip cost.",
     "parameters": {"type": "object", "properties": {"origin": {"type": "string", "enum": list(CITIES)},
                                                     "dest": {"type": "string", "enum": list(CITIES)}},
                    "required": ["origin", "dest"]}},
    {"name": "ask_fleet_analyst", "description": "Answer analytics questions over 90 days of trip history (BigQuery data agent).",
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
]
DRIVER_TOOLS = [
    {"name": "get_my_trip", "description": "The driver's current truck, order, destination, ETA and hours driven."},
    OWNER_TOOLS[2],
    {"name": "report_issue", "description": "Report a problem to the owner and the agent team.",
     "parameters": {"type": "object", "properties": {"issue": {"type": "string",
                    "enum": ["breakdown", "delivery_refused", "detention", "fatigue"]}}, "required": ["issue"]}},
]


def fleet_summary():
    k = fleet.kpis()
    return {"kpis": k, "trucks": [{"id": t["id"], "driver": t["driver"], "status": t["status"], "order": t["order"]}
                                  for t in fleet.trucks.values()],
            "open_orders": [{"id": o["id"], "route": f"{o['from']}->{o['to']}", "status": o["status"], "eta_h": o["eta_h"]}
                            for o in fleet.orders.values() if o["status"] not in ("delivered", "cancelled")]}


async def exec_tool(name, args, truck_id=None):
    if name == "get_fleet_status":
        return fleet_summary()
    if name == "run_playbook":
        return await engine.run(args.get("name"), args.get("target") or None)
    if name == "predict_route":
        p = await predict_trip(args["origin"], args["dest"])
        bus.publish("route", {"route": p})
        return {k: p[k] for k in ("km", "eta_hours", "arrive", "best_departure", "cost", "risk_score", "advice")} | \
            {"weather": [w["condition"] for w in p["weather"]]}
    if name == "ask_fleet_analyst":
        r = await analyst.ask(args["question"])
        bus.publish("analyst", r)
        return {"answer": r["answer"], "rows": r["rows"][:8]}
    if name == "get_my_trip":
        t = fleet.trucks.get(truck_id)
        o = fleet.orders.get(t["order"]) if t and t["order"] else None
        return {"truck": t and t["id"], "driver": t and t["driver"], "hours_driven": t and t["drive_hours"],
                "order": o and {k: o[k] for k in ("id", "from", "to", "cargo", "eta_h", "consignee")}}
    if name == "report_issue":
        return await engine.run(args["issue"], truck_id)
    return {"error": "unknown tool"}


def fallback_intent(text: str, role: str):
    s = text.lower()
    rules = [("breakdown|kharab|band ho|overheat|puncture|tyre", "breakdown"), ("empty|khaali|khali|wapas", "empty_return"),
             ("refus|mana|reject|nahi le", "delivery_refused"), ("document|insurance|puc|permit|fitness|kagaz", "docs_expiring"),
             ("cancel", "cancel_after_dispatch"), ("detention|wait|intezaar|ghante se|loading nahi", "detention"),
             ("thak|neend|tired|sleep", "fatigue"), ("fastag|toll", "fastag_audit"),
             ("sentinel|sab theek|sab thik|everything ok|checklist", "sentinel"),
             ("new order|naya order|dispatch|bhejo", "dispatch")]
    for pat, pb in rules:
        if re.search(pat, s):
            if role == "driver" and pb not in ("breakdown", "delivery_refused", "detention", "fatigue"):
                continue
            return ("playbook", pb)
    if role == "driver" and re.search(r"trip|baaki|kitna|door|pahunch|eta", s):
        return ("trip", None)
    if re.search(r"route|eta|mausam|weather|kitna time|traffic", s):
        return ("route", None)
    if re.search(r"profit|loss|nuksaan|munafa|diesel|mileage|trend|kamai|on.?time", s):
        return ("analyst", None)
    return ("status", None)


RELAY_TO_OWNER = re.compile(r"(malik|owner|seth|office|sir)\s*(ji)?\s*(ko|se)\s*(bolo|bol do|bata do|batao|kaho|keh do)\s*(ki|that)?\s*", re.I)
RELAY_TO_DRIVER = re.compile(r"(?:(RJ\s?\d{2}\s?[A-Z]{1,3}\s?\d{3,4})|(\w+))\s*(?:ke|ka|wale)?\s*driver\s*(?:ko)\s*(?:bolo|bol do|bata do|batao|kaho)\s*(?:ki|that)?\s*", re.I)


async def try_relay(text, role, truck):
    if role == "driver":
        m = RELAY_TO_OWNER.search(text)
        if not m:
            return None
        msg = text[m.end():].strip() or text
        t = fleet.trucks.get(truck)
        name = t["driver"] if t else "Driver"
        await engine.say("core", f"📨 Message from {name} ({truck}) for the owner: \"{msg}\"", kind="relay", wait=0, truck=truck)
        return f"{name.split()[0]} ji, malik ko bata diya: \"{msg}\". Jawab aate hi main sunaunga."
    m = RELAY_TO_DRIVER.search(text)
    if not m:
        return None
    key = (m.group(1) or m.group(2) or "").replace(" ", "").upper()
    t = fleet.trucks.get(key) or next((x for x in fleet.trucks.values() if x["driver"].split()[0].upper() == key), None)
    if not t:
        t = next((x for x in fleet.trucks.values() if x["status"] == "on_the_way"), None)
    msg = text[m.end():].strip() or text
    await engine.say("core", f"📨 Owner → {t['driver']} ({t['id']}): \"{msg}\"", kind="relay", wait=0, truck=t["id"])
    await engine.dash(t["id"], f"{t['driver'].split()[0]} ji, malik ka sandesh: {msg}")
    return f"Sent to {t['driver']} on {t['id']} - Saarthi Dash is reading it out on his dashboard now."


# --------------------------------------------------------------------------- routes
async def index(_):
    return web.FileResponse(STATIC / "home.html")


async def fleet_page(_):
    return web.FileResponse(STATIC / "index.html")


async def ceo_page(_):
    return web.FileResponse(STATIC / "ceo.html")


async def dash_page(_):
    return web.FileResponse(STATIC / "dash.html")


async def api_state(_):
    return web.json_response(fleet.snapshot() | {"gemini": gemini.status(), "events": bus.events[-60:],
                                                 "analyst_engine": "BigQuery" if analyst.bq else "local mirror"})


async def api_events(request):
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "Connection": "keep-alive"})
    await resp.prepare(request)
    q = bus.subscribe()
    try:
        await resp.write(b": hi\n\n")
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
                await resp.write(f"data: {json.dumps(msg, default=str)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        bus.subs.discard(q)
    return resp


async def api_playbook(request):
    body = await request.json()
    return web.json_response(await engine.run(body.get("name"), body.get("target")))


async def api_chat(request):
    body = await request.json()
    text, role, truck = body.get("text", "").strip(), body.get("role", "owner"), body.get("truck")
    if not text:
        return web.json_response({"reply": ""})
    who = "dash" if role == "driver" else "core"
    if role == "driver":
        t = fleet.trucks.get(truck)
        system = DRIVER_SYSTEM.replace("<name>", t["driver"].split()[0] if t else "Bhai")
        tools = DRIVER_TOOLS
    elif role == "ceo":
        system, tools = CEO_SYSTEM, OWNER_TOOLS
    else:
        system, tools = OWNER_SYSTEM, OWNER_TOOLS
    relay = await try_relay(text, role, truck)  # merged from the Gnani build: driver <-> owner messages
    if relay:
        await engine.say(who, f"\"{text}\"", kind="user", wait=0, role=role, truck=truck)
        await engine.say(who, relay, kind="reply", wait=0, role=role, truck=truck)
        return web.json_response({"reply": relay, "tools": [{"tool": "relay_message", "args": {}}], "ai": gemini.ready()})
    done: list = []

    async def ex(n, a):
        r = await exec_tool(n, a, truck)
        done.append((n, a, r))
        return r

    reply, calls = await gemini.chat_with_tools([{"role": "user", "text": text}], system, tools, ex)
    if reply is None and done:  # Gemini acted but timed out before replying: report what was done, never repeat it
        n, a, r = done[-1]
        if n == "ask_fleet_analyst":
            reply = r.get("answer", "Analysis ready - see the chart.")
        elif n in ("run_playbook", "report_issue"):
            pb = a.get("name") or a.get("issue", "")
            reply = f"On it - the {pb.replace('_', ' ')} playbook is running. Watch the agent feed."
        elif n == "predict_route":
            reply = f"{r['km']} km, ETA {r['eta_hours']} h, trip cost about Rs {r['cost']['total']:,}. {r['advice'][0]}"
        elif n == "get_my_trip" and r.get("order"):
            reply = f"{r['order']['to']} tak lagbhag {r['order']['eta_h']} ghante baaki hain."
        else:
            k = fleet.kpis()
            reply = f"{k['on_road']} trucks on the road, {k['loading']} loading, {k['empty']} free."
    if reply is None:  # offline / error -> deterministic intent routing keeps the demo working
        kind, arg = fallback_intent(text, role)
        if kind == "playbook":
            await engine.run(arg, truck if role == "driver" else None)
            reply = "Theek hai, team ko laga diya hai - updates live aa rahe hain." if role == "driver" else \
                f"On it - the {arg.replace('_', ' ')} playbook is running. Watch the agent feed."
        elif kind == "route":
            o = fleet.orders.get(fleet.trucks[truck]["order"]) if role == "driver" and truck in fleet.trucks and fleet.trucks[truck]["order"] else None
            named = sorted((c for c in CITIES if c.lower() in text.lower()), key=lambda c: text.lower().index(c.lower()))
            a, b = (named[0], named[1]) if len(named) >= 2 else ((o["from"], o["to"]) if o else ("Chittorgarh", named[0] if named else "Ahmedabad"))
            p = await predict_trip(a, b)
            bus.publish("route", {"route": p})
            reply = f"{a} to {b}: {p['km']} km, ETA {p['eta_hours']} h. Weather {p['weather'][1]['condition']}. {p['advice'][0]}"
        elif kind == "trip":
            t = fleet.trucks.get(truck)
            o = fleet.orders.get(t["order"]) if t and t["order"] else None
            name = t["driver"].split()[0] if t else "Bhai"
            reply = (f"{name} ji, {o['to']} abhi {round(o['km'] * (1 - o['progress']))} km door hai, lagbhag {o['eta_h']} ghante. "
                     f"Aap aaj {t['drive_hours']} ghante chala chuke ho." if o else f"{name} ji, abhi koi trip assign nahi hai.")
        elif kind == "analyst":
            r = await analyst.ask(text)
            bus.publish("analyst", r)
            reply = r["answer"]
        else:
            k = fleet.kpis()
            reply = (f"{k['on_road']} trucks on the road, {k['loading']} loading, {k['empty']} free, "
                     f"{k['breakdown']} breakdowns. Rs {k['revenue_recovered']:,} recovered today.")
    await engine.say(who, f"\"{text}\"", kind="user", wait=0, role=role, truck=truck)
    await engine.say(who, reply, kind="reply", wait=0, role=role, truck=truck, tools=calls)
    return web.json_response({"reply": reply, "tools": calls, "ai": gemini.ready()})


CEO_SYSTEM = OWNER_SYSTEM + """
You are speaking as the owner's CEO Agent: think like a business partner. Lead with money (profit, loss, cash),
then the action you recommend or have taken. If the owner asks for a brief, call get_fleet_status and ask_fleet_analyst."""


def _q(sql):
    try:
        return analyst._run(sql)[1]
    except Exception:
        return []


async def api_ceo_brief(_):
    k = fleet.kpis()
    low_profit = _q("SELECT truck_id, SUM(freight_rs-diesel_rs-toll_rs-driver_rs) p FROM trips "
                    "WHERE trip_date >= DATE('now','-30 day') GROUP BY truck_id ORDER BY p ASC LIMIT 1")
    low_kmpl = _q("SELECT truck_id, ROUND(SUM(loaded_km+empty_km)/SUM(diesel_litres),2) k, "
                  "(SELECT ROUND(SUM(loaded_km+empty_km)/SUM(diesel_litres),2) FROM trips) f FROM trips "
                  "GROUP BY truck_id ORDER BY k ASC LIMIT 1")
    hi_empty = _q("SELECT truck_id, ROUND(100.0*SUM(empty_km)/SUM(loaded_km+empty_km),1) e FROM trips "
                  "GROUP BY truck_id ORDER BY e DESC LIMIT 1")
    profit30 = _q("SELECT SUM(freight_rs-diesel_rs-toll_rs-driver_rs), SUM(freight_rs) FROM trips "
                  "WHERE trip_date >= DATE('now','-30 day')")
    docs = fleet.docs_report(15)
    proposals = []
    if low_kmpl:
        t, kmpl, fleet_kmpl = low_kmpl[0]
        gap = round(100 * (1 - kmpl / fleet_kmpl))
        proposals.append({"id": "service", "truck": t, "title": f"Inspect {t} for diesel loss",
                          "detail": f"Mileage {kmpl} km/l vs fleet {fleet_kmpl} ({gap}% worse): leak or pilferage risk. "
                                    f"Book a fuel-system check and seal the tank.", "impact": f"~Rs {gap * 900:,}/month"})
    if hi_empty:
        proposals.append({"id": "empty", "truck": hi_empty[0][0], "title": f"Stop empty running on {hi_empty[0][0]}",
                          "detail": f"{hi_empty[0][1]}% of its kilometres are empty. Let Freight Matcher book return loads automatically.",
                          "impact": "Rs 35-60k per return load"})
    if docs:
        proposals.append({"id": "docs", "title": f"Renew {len(docs)} expiring documents",
                          "detail": ", ".join(f"{d['truck']} {d['doc']} ({d['days_left']}d)" for d in docs[:3]),
                          "impact": "avoid fines + blocked trips"})
    facts = {"kpis": k, "lowest_profit_truck_30d": low_profit, "lowest_mileage": low_kmpl, "highest_empty_pct": hi_empty,
             "fleet_profit_30d_and_revenue": profit30, "expiring_docs": len(docs)}
    brief = await gemini.generate(
        "Write a 3-sentence morning business brief for an Indian truck fleet owner from these facts. "
        "Start with 'Good morning'. Lead with 30-day profit, then today's live situation, then the top recommendation. "
        f"Use Rs figures, simple English. Facts: {json.dumps(facts, default=str)}", timeout=12)
    if not brief:
        p = profit30[0][0] if profit30 else 0
        brief = (f"Good morning. Last 30 days profit is Rs {int(p or 0):,}. Right now {k['on_road']} trucks are on the road, "
                 f"{k['loading']} loading and {k['empty']} free. Top action: {proposals[0]['title'] if proposals else 'none'}.")
    return web.json_response({"brief": brief, "kpis": k, "proposals": proposals,
                              "profit_30d": int(profit30[0][0] or 0) if profit30 else 0,
                              "revenue_30d": int(profit30[0][1] or 0) if profit30 else 0,
                              "low_profit": low_profit[0] if low_profit else None, "ai": gemini.ready()})


async def api_ceo_approve(request):
    body = await request.json()
    pid, truck = body.get("id"), body.get("truck")
    if pid == "service":
        await engine.say("core", f"CEO approved: fuel-system inspection for {truck}.", kind="user", wait=0)
        asyncio.create_task(_service(truck))
        return web.json_response({"ok": True, "msg": f"Service booked for {truck}"})
    if pid == "empty":
        return web.json_response(await engine.run("empty_return", None) | {"msg": "Freight Matcher started"})
    if pid == "docs":
        return web.json_response(await engine.run("docs_expiring", None) | {"msg": "Compliance Agent started"})
    return web.json_response({"ok": False})


async def _service(truck):
    await engine.say("recovery", f"Booking fuel-system inspection for {truck} at Mewar Auto Garage, tomorrow 09:00 (simulated).",
                     truck=truck)
    await engine.dash(truck, "Kal subah 9 baje Mewar Auto Garage mein diesel tank aur pipe check hoga. Trip ke baad seedha wahan aaiye.")
    await engine.resolve("service", f"Diesel inspection booked - {truck}", 0, {"truck": truck},
                         f"Fuel-system inspection booked for {truck}; tank will be sealed after the check.")


async def api_sentinel(_):
    rows = sentinel_report(fleet)
    return web.json_response({"rows": rows, "ok": sum(r["ok"] for r in rows), "total": len(rows)})


async def api_analyst(request):
    body = await request.json()
    r = await analyst.ask(body.get("question", "Which truck made the least profit this month?"))
    bus.publish("analyst", r)
    return web.json_response(r)


async def api_predict(request):
    o, d = request.query.get("from", "Chittorgarh"), request.query.get("to", "Ahmedabad")
    if o not in CITIES or d not in CITIES:
        return web.json_response({"error": "unknown city"}, status=400)
    p = await predict_trip(o, d)
    bus.publish("route", {"route": p})
    return web.json_response(p)


async def api_reset(_):
    fleet.reset()
    bus.events.clear()
    engine.running.clear()
    engine.push_state()
    return web.json_response({"ok": True})


async def api_pace(request):
    body = await request.json()
    engine.pace = max(0.2, min(3.0, float(body.get("pace", 1.0))))
    return web.json_response({"pace": engine.pace})


async def api_health(_):
    return web.json_response({"ok": True, "gemini": gemini.status(), "analyst": "BigQuery" if analyst.bq else "local"})


async def ticker(app):
    async def loop():
        while True:
            await asyncio.sleep(2)
            fleet.tick(0.03)
            bus.publish("state", fleet.snapshot())
    task = asyncio.create_task(loop())
    yield
    task.cancel()


@web.middleware
async def no_cache(request, handler):
    resp = await handler(request)
    if not request.path.startswith("/api/events"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def make_app():
    gemini.init()
    app = web.Application(middlewares=[no_cache])
    app.router.add_get("/", index)
    app.router.add_get("/dash", dash_page)
    app.router.add_get("/fleet", fleet_page)
    app.router.add_get("/ceo", ceo_page)
    app.router.add_get("/api/ceo/brief", api_ceo_brief)
    app.router.add_post("/api/ceo/approve", api_ceo_approve)
    app.router.add_get("/api/ceo/sentinel", api_sentinel)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/events", api_events)
    app.router.add_post("/api/playbook", api_playbook)
    app.router.add_post("/api/chat", api_chat)
    app.router.add_post("/api/analyst", api_analyst)
    app.router.add_get("/api/predict", api_predict)
    app.router.add_post("/api/reset", api_reset)
    app.router.add_post("/api/pace", api_pace)
    app.router.add_get("/api/health", api_health)
    app.router.add_static("/static", STATIC)
    app.cleanup_ctx.append(ticker)
    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    application = make_app()
    print(f"Saarthi AI on http://localhost:{port}  | Gemini: {gemini.status()}", flush=True)
    web.run_app(application, port=port, print=None)
