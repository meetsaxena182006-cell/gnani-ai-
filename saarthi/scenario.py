"""DEMO SCENARIO — a complete, SIMULATED trip Udaipur (Rajasthan) -> Hyderabad (Telangana) for the video.

Everything here is demo data: truck, driver, client, cargo, money, traffic, weather, mechanics and phone numbers are
made up (labelled "(demo)"). Toll plazas come from the public NHAI dataset (2019-20 rates). The truck position is
simulated; every reaction comes from the real system (CEO watchdog, FASTag toll logging, breakdown flow, Fleet Mesh
agent-to-agent help, planner, invoices) plus scripted announcements for events that need the outside world.

Driven from the office page ("Demo trip" panel): Start -> Next (one event at a time) or Auto (plays all).
The driver's phone (MW-T1 link) hears Saarthi live; the owner's CEO voice call (office page) hears the CEO.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import db, demo_drive, fatigue, geo, tolls
from .agents.common import inr, ist_now
from .bus import Out, bus, to_driver

log = logging.getLogger("saarthi.scenario")

HERO, HELPER = "MW-T1", "SG-T1"
DEST = (17.4400, 78.5000)
CLIENT = "Deccan Stone Traders (demo)"
CARGO = "18 टन मार्बल स्लैब (demo)"
FREIGHT, ADVANCE = 118000, 30000

# demo route (approximate highway towns): NH27/NH52 to Indore, NH753 via Burhanpur-Akola, NH161 Nanded, NH44 Hyderabad
WAYPOINTS = [("Udaipur", 24.585, 73.712), ("Mangalwad", 24.75, 74.35), ("Chittorgarh", 24.879, 74.629),
             ("Nimbahera", 24.62, 74.68), ("Neemuch", 24.47, 74.87), ("Mandsaur", 24.07, 75.07), ("Jaora", 23.64, 75.13),
             ("Badnawar", 23.02, 75.23), ("Indore", 22.72, 75.86), ("Khandwa", 21.82, 76.35), ("Burhanpur", 21.31, 76.23),
             ("Malkapur", 20.88, 76.20), ("Akola", 20.70, 77.00), ("Washim", 20.11, 77.13), ("Hingoli", 19.72, 77.15),
             ("Nanded", 19.15, 77.31), ("Bhainsa", 19.10, 77.96), ("Nizamabad", 18.67, 78.09),
             ("Kamareddy", 18.32, 78.34), ("Medchal", 17.63, 78.48), ("Hyderabad", 17.44, 78.50)]

S = {"running": False, "step": -1, "auto": False, "log": [], "km": 0.0, "weather": "clear", "task": None}
_route: list[tuple[float, float]] = []
_cum: list[float] = []
_saved_geo: dict = {}


# ---------------- route with real NHAI plazas on it ----------------
def _build_route() -> None:
    """Densify the demo line and pass exactly through NHAI toll plazas lying near it (so FASTag logging fires)."""
    global _route, _cum
    pts = [(la, lo) for _, la, lo in WAYPOINTS]
    dense: list[tuple[float, float]] = []
    for (a, b), (c, d) in zip(pts, pts[1:]):
        n = max(1, int(geo.haversine_km(a, b, c, d) / 4))
        dense += [(a + (c - a) * i / n, b + (d - b) * i / n) for i in range(n)]
    dense.append(pts[-1])
    near = []
    for p in tolls.plazas():
        best = min(range(len(dense)), key=lambda i: abs(dense[i][0] - p["lat"]) + abs(dense[i][1] - p["lon"]))
        if geo.haversine_km(p["lat"], p["lon"], *dense[best]) <= 20 and tolls.expected_fee(p, "truck_10w"):
            near.append((best, p))
    seen, extra = set(), {}
    for i, p in sorted(near, key=lambda x: x[0]):
        if p["name"] not in seen:
            seen.add(p["name"])
            extra.setdefault(i, []).append((p["lat"], p["lon"]))
    _route = []
    for i, pt in enumerate(dense):
        _route.append(pt)
        _route += extra.get(i, [])
    _cum = [0.0]
    for (a, b), (c, d) in zip(_route, _route[1:]):
        _cum.append(_cum[-1] + geo.haversine_km(a, b, c, d))


def total_km() -> float:
    return _cum[-1] if _cum else 0.0


def km_of(town: str) -> float:
    la, lo = next((la, lo) for n, la, lo in WAYPOINTS if n == town)
    i = min(range(len(_route)), key=lambda k: abs(_route[k][0] - la) + abs(_route[k][1] - lo))
    return _cum[i]


def _point(km: float) -> tuple[float, float]:
    for i in range(1, len(_cum)):
        if _cum[i] >= km:
            f = (km - _cum[i - 1]) / max(1e-6, _cum[i] - _cum[i - 1])
            (a, b), (c, d) = _route[i - 1], _route[i]
            return a + (c - a) * f, b + (d - b) * f
    return _route[-1]


def _remaining(km: float) -> list[tuple[float, float]]:
    return [_point(km)] + [p for p, c in zip(_route, _cum) if c > km]


# ---------------- demo outside world (traffic, weather, places) ----------------
def _install_demo_world() -> None:
    if _saved_geo:
        return
    for k in ("route", "weather", "forecast_at", "forecast_hourly", "overpass_along", "nearby"):
        _saved_geo[k] = getattr(geo, k)

    async def route(o_lat, o_lon, d_lat, d_lon):
        coords = _remaining(S["km"])
        dist = sum(geo.haversine_km(*a, *b) for a, b in zip(coords, coords[1:]))
        delay = 40 if S.get("jam") else 0
        hours = dist / 48 + delay / 60
        alt = dist * 1.06
        return {"source": "demo route (simulated)", "traffic": "demo traffic" + (" — jam at Indore bypass" if delay else ""),
                "routes": [{"distance_km": round(dist), "duration_h": round(dist / 60, 1), "truck_duration_h": round(hours, 1),
                            "traffic_delay_min": delay, "sample_points": coords[::max(1, len(coords) // 5)][:6], "_coords": coords},
                           {"distance_km": round(alt), "duration_h": round(alt / 60, 1), "truck_duration_h": round(alt / 46, 1),
                            "_coords": coords}]}

    def wx():
        if S["weather"] == "rain":
            return {"now": "heavy rain (demo)", "temp_c": 24, "visibility_m": 900, "wind_kmph": 38,
                    "warnings": ["HEAVY RAIN: braking distance doubles", "Rain likely in the next few hours"]}
        return {"now": "clear sky (demo)", "temp_c": 29, "visibility_m": 10000, "wind_kmph": 12, "warnings": []}

    async def weather(lat, lon):
        return wx()

    async def forecast_at(lat, lon, hours_ahead):
        w = wx()
        w["at"] = (ist_now() + timedelta(hours=hours_ahead)).strftime("%Y-%m-%dT%H:00")
        return w

    async def forecast_hourly(lat, lon, days=3):
        return {}

    async def overpass_along(points, kinds=("fuel", "dhaba"), radius_m=3000):
        return [[{"name": f"Demo Highway Dhaba & Fuel #{i + 1}", "distance_km": 0.6, "lat": la, "lon": lo,
                  "maps": geo.maps_link(la, lo)}] for i, (la, lo) in enumerate(points)]

    async def nearby(kind, lat, lon, radius_m=15000, limit=5):
        names = {"tyre": ["Akola Demo Tyre Works", "Sharma Puncture Point (demo)"],
                 "mechanic": ["Demo Diesel Mechanic", "Highway Truck Garage (demo)"],
                 "fuel": ["Demo Fuel Station"], "dhaba": ["Demo Highway Dhaba"]}.get(kind, [f"Demo {kind} help"])
        return [{"name": n, "phone": f"+9100000000{k + 1:02d}", "distance_km": round(2.4 + 3.1 * k, 1),
                 "lat": lat + 0.02 * (k + 1), "lon": lon + 0.01, "maps": geo.maps_link(lat + 0.02 * (k + 1), lon + 0.01)}
                for k, n in enumerate(names[:limit])]

    geo.route, geo.weather, geo.forecast_at = route, weather, forecast_at
    geo.forecast_hourly, geo.overpass_along, geo.nearby = forecast_hourly, overpass_along, nearby


def _remove_demo_world() -> None:
    for k, f in _saved_geo.items():
        setattr(geo, k, f)
    _saved_geo.clear()


# ---------------- helpers ----------------
def hero() -> dict:
    return db.vehicle_by_code(HERO)


def note(text: str) -> None:
    S["log"].append({"t": ist_now().strftime("%H:%M:%S"), "text": text})
    S["log"] = S["log"][-60:]
    db.log_agent("scenario", "review", "demo_step", {"text": text})


async def driver_say(text: str, card: str | None = None, **kw) -> None:
    """Saarthi speaks to the driver (live phone) and it shows on the screen + office."""
    await bus.send(to_driver(hero(), card or text, speak=text, **kw))


async def ceo_tell(text: str, speak: str | None = None) -> None:
    """CEO Agent -> owner: office inbox always; spoken too if the owner is on the CEO voice call."""
    v = hero()
    f = db.fleet(v["fleet_id"])
    if f.get("owner_chat_id"):
        await bus.send(Out(chat_id=f["owner_chat_id"], text=text, speak=speak, channel="owner"))
    else:
        db.log_agent("system", f"owner:{f['code']}", "message", {"text": text})
    try:
        from . import owner_voice
        s = owner_voice._sessions.get(f["id"])
        if s and s.worker and speak:
            from pipecat.frames.frames import TTSSpeakFrame
            await s.worker.queue_frames([TTSSpeakFrame(speak)])
    except Exception:
        log.exception("CEO voice update failed")


def expense(cat: str, amount: float, note_: str, litres: float | None = None) -> None:
    v = hero()
    t = db.active_trip(v["id"])
    db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,litres,note,lat,lon,status,created_at) "
          "VALUES(?,?,?,?,?,?,?,?, 'confirmed', ?)",
          (v["id"], t["id"] if t else None, cat, amount, litres, note_, v["last_lat"], v["last_lon"], db.now()))


def money() -> dict:
    v = hero()
    t = db.active_trip(v["id"]) or db.one("SELECT * FROM trips WHERE vehicle_id=? ORDER BY id DESC LIMIT 1", (v["id"],))
    rows = db.q("SELECT category, SUM(amount) s FROM expenses WHERE trip_id=? AND status='confirmed' GROUP BY category", (t["id"],))
    m = {r["category"]: round(r["s"] or 0) for r in rows}
    spent = sum(val for k, val in m.items() if k not in ("freight_advance", "freight"))
    m["spent"] = spent
    m["profit"] = round(FREIGHT - spent)
    m["toll_count"] = db.one("SELECT COUNT(*) c FROM toll_crossings WHERE trip_id=?", (t["id"],))["c"]
    return m


async def drive_to(km_target: float, seconds: float = 10.0, speed: float = 58.0) -> None:
    v = hero()
    start = S["km"]
    km_target = min(km_target, total_km())
    # pass through every route vertex (toll plazas are vertices), then the exact target
    marks = [c for c in _cum if start < c < km_target] + [km_target]
    if S.get("film") is not None:      # film capture: remember the drive so the playback can animate it
        S["film"].append({"kind": "move", "from": round(start, 1), "to": round(km_target, 1), "speed": speed})
    for km in marks:
        S["km"] = km
        la, lo = _point(km)
        await fatigue.location_update(v["id"], la, lo, speed)
        if not S.get("fast"):
            await asyncio.sleep(seconds / len(marks))


async def stop_here() -> None:
    la, lo = _point(S["km"])
    await fatigue.location_update(hero()["id"], la, lo, 0)


def eta_text(extra_h: float = 0) -> str:
    left = total_km() - S["km"]
    return (ist_now() + timedelta(hours=left / 48 + extra_h)).strftime("%I:%M %p")


# ---------------- the story ----------------
async def s_setup():
    _build_route()
    _install_demo_world()
    v = hero()
    demo_drive.SIM_IDS.add(v["id"])             # phone GPS ignored: the simulated route is in charge
    helper = db.vehicle_by_code(HELPER)
    old = db.active_trip(v["id"])
    if old:
        db.ex("UPDATE trips SET status='closed' WHERE id=?", (old["id"],))
    db.ex("DELETE FROM toll_crossings WHERE vehicle_id=?", (v["id"],))
    db.ex("UPDATE incidents SET status='resolved' WHERE vehicle_id=?", (v["id"],))
    db.ex("UPDATE mesh_requests SET status='closed' WHERE requester_vehicle=? AND status IN ('open','matched')", (v["id"],))
    db.ex("UPDATE vehicles SET driver_name='Ramesh (demo)', fastag=1, driving_since=NULL WHERE id=?", (v["id"],))
    # helper truck of the OTHER fleet parked near Akola: its own Saarthi agent answers help requests
    db.ex("UPDATE vehicles SET driver_name='Suresh (demo)', driver_chat_id=COALESCE(driver_chat_id, -21) WHERE id=?", (helper["id"],))
    db.ex("UPDATE fleets SET mesh_opt_in=1")
    S.update(km=0.0, weather="clear", jam=False)
    la, lo = _point(0)
    await fatigue.location_update(v["id"], la, lo, 0)
    t = db.start_trip(v["id"], "Udaipur", "Hyderabad", FREIGHT, CLIENT)
    db.ex("UPDATE trips SET origin_lat=?, origin_lon=?, dest_lat=?, dest_lon=?, eway_bill='EWB-DEMO-TS-4401', "
          "eway_valid_until=? WHERE id=?", (la, lo, DEST[0], DEST[1],
          (datetime.now(timezone.utc) + timedelta(days=6)).isoformat(timespec="minutes"), t["id"]))
    expense("freight_advance", ADVANCE, "advance from client (demo)")
    # PUC expiring soon (so the CEO has a paper to handle on the way)
    db.ex("DELETE FROM documents WHERE vehicle_id=? AND doc_type='PUC'", (v["id"],))
    db.ex("INSERT INTO documents(vehicle_id,doc_type,expiry_date) VALUES(?, 'PUC', ?)",
          (v["id"], (datetime.now().date() + timedelta(days=4)).isoformat()))
    note(f"Demo trip ready: {v['reg_label']} Udaipur → Hyderabad, {total_km():.0f} km demo route")
    await ceo_tell(f"🆕 New trip (demo): {v['reg_label']} · Udaipur → Hyderabad · {CARGO} · client {CLIENT} · "
                   f"freight {inr(FREIGHT)} · advance {inr(ADVANCE)} received · e-way bill EWB-DEMO-TS-4401.",
                   speak=f"मालिक, नया ट्रिप लग गया है। RJ00 DEMO 01 उदयपुर से हैदराबाद जाएगी, अठारह टन मार्बल, "
                         f"भाड़ा एक लाख अठारह हज़ार, तीस हज़ार एडवांस आ गया है।")


async def s_brief():
    from . import costing, planner
    v = hero()
    km = total_km()
    p = costing.PROFILES[v["vtype"]]
    litres = km / p.kmpl_loaded
    diesel_rs = litres * db.fleet(v["fleet_id"])["diesel_price"]
    toll = tolls.on_route(_route, v["vtype"])
    toll_total = toll.get("expected_total") or 0
    n_tolls = len(toll.get("plazas", []))
    hours = km / 48
    try:
        plan = await planner.plan_for_vehicle(v, notify=False)
    except Exception:
        log.exception("planner failed")
        plan = {}
    S["plan"] = {"km": round(km), "litres": round(litres), "diesel": round(diesel_rs), "tolls": n_tolls,
                 "toll_total": round(toll_total or 0), "hours": round(hours, 1)}
    note(f"Plan: {km:.0f} km · ~{hours:.0f} h driving · diesel ~{litres:.0f} L ({inr(diesel_rs)}) · "
         f"{n_tolls} toll plazas ({inr(toll_total or 0)}, NHAI 2019-20 rates) · 2 fuel stops · rest every 4 h")
    await driver_say(
        f"राम राम रमेश भाई! आज लंबा सफ़र है — उदयपुर से हैदराबाद, करीब {km:.0f} किलोमीटर। मैंने पूरा प्लान बना लिया है: "
        f"चित्तौड़, इंदौर, अकोला, नांदेड़ होते हुए निज़ामाबाद। रास्ते में {n_tolls} टोल हैं, FASTag से अपने-आप कटेंगे, "
        f"करीब {inr(toll_total or 0)}। डीज़ल लगभग {litres:.0f} लीटर लगेगा, दो बार भरवाएंगे — नीमच के बाद और अकोला से पहले। "
        f"हर चार घंटे में चाय-ब्रेक। सब मालिक को भी भेज दिया है। चलिए, गाड़ी निकालिए, मैं साथ हूँ!",
        card=f"🗺️ Udaipur → Hyderabad · {km:.0f} km · {n_tolls} tolls · ~{litres:.0f} L diesel · ETA ~{hours:.0f} h driving")
    await ceo_tell(f"🗺️ Trip plan (demo) MW-T1: {km:.0f} km, ~{hours:.0f} h driving + rests. Diesel ~{litres:.0f} L "
                   f"≈ {inr(diesel_rs)}. Tolls: {n_tolls} plazas ≈ {inr(toll_total or 0)}. Fuel stops: after Neemuch, before Akola. "
                   f"Expected profit before driver pay ≈ {inr(FREIGHT - diesel_rs - (toll_total or 0))}.",
                   speak=f"प्लान तैयार है मालिक। {km:.0f} किलोमीटर, डीज़ल करीब {inr(diesel_rs)}, टोल करीब {inr(toll_total or 0)}। "
                         f"इस ट्रिप में अंदाज़न {inr(FREIGHT - diesel_rs - (toll_total or 0))} बचेंगे।")


async def s_depart():
    await driver_say("बढ़िया! सफ़र शुरू। लाइव लोकेशन ऑफिस को जा रही है। कोई भी बात हो, बस बोल दीजिए।")
    await drive_to(km_of("Chittorgarh"), 10)
    m = money()
    note(f"Chittorgarh crossed · tolls auto-logged via FASTag: {m.get('toll_count', 0)} ({inr(m.get('toll', 0))})")
    await driver_say(f"चित्तौड़गढ़ पार! किला दिख रहा है ना बाएं — कभी परिवार के साथ आइए यहाँ। "
                     f"अब तक {m.get('toll_count', 0)} टोल FASTag से कटे, {inr(m.get('toll', 0))}, हिसाब में चढ़ा दिया।")
    await ceo_tell(f"📍 MW-T1 crossed Chittorgarh. Tolls so far {inr(m.get('toll', 0))} (FASTag, auto-logged).")


async def s_fuel():
    await drive_to(km_of("Neemuch") + 12, 8)
    await stop_here()
    note("Fuel stop near Neemuch (demo pump)")
    await driver_say("रमेश भाई, आगे Demo Fuel Station है — यहीं डीज़ल भरवा लीजिए, आगे सौ किलोमीटर तक सस्ता पंप नहीं है। "
                     "भरवा के बस बोल दीजिए — कितने लीटर, कितने रुपये। मैं लिख लूँगा।")


async def s_fuel_done():
    v = hero()
    t = db.active_trip(v["id"])
    got = db.one("SELECT id FROM expenses WHERE trip_id=? AND category='diesel'", (t["id"],))
    if not got:  # the driver did not say it by voice: demo entry so the story continues
        expense("diesel", 21620, "Demo Fuel Station, Neemuch (demo)", litres=235)
        await driver_say("डीज़ल 235 लीटर, इक्कीस हज़ार छह सौ बीस रुपये — लिख लिया, बिल की फोटो बाद में ले लेंगे।")
    m = money()
    note(f"Diesel logged · trip spend so far {inr(m['spent'])}")
    await ceo_tell(f"⛽ Diesel at Neemuch logged. Trip spend so far {inr(m['spent'])} (diesel {inr(m.get('diesel', 0))}, "
                   f"tolls {inr(m.get('toll', 0))}). Mileage on track.")


async def s_traffic():
    await drive_to(km_of("Indore") - 18, 12)
    S["jam"] = True
    note("Traffic: 40 min jam on Indore bypass (demo) → reroute via ring road saves ~25 min")
    await driver_say("अरे रुकिए! आगे इंदौर बायपास पे चालीस मिनट का जाम है — एक ट्रक पलटा है। "
                     "मैंने रास्ता बदल दिया: अगले कट से रिंग रोड लीजिए, पच्चीस मिनट बचेंगे और डीज़ल भी कम जलेगा। "
                     f"नया पहुँचने का समय: कल {eta_text(0.25)}। ऑफिस को बता दिया।",
                     card="🚦 Jam ahead (Indore bypass, demo) → take ring road (−25 min)")
    await ceo_tell("🚦 Jam on Indore bypass (demo). I re-routed MW-T1 via ring road: saves ~25 min and ~6 L diesel. ETA updated.",
                   speak="इंदौर बायपास पे जाम था, मैंने गाड़ी को रिंग रोड से निकलवा दिया, पच्चीस मिनट बचेंगे।")
    await drive_to(km_of("Indore") + 10, 6)
    S["jam"] = False


async def s_weather():
    await drive_to(km_of("Khandwa") + 20, 10)
    S["weather"] = "rain"
    note("Weather: heavy rain ahead Burhanpur ghat (demo) → speed 40, hazard check")
    await driver_say("रमेश भाई, ध्यान दीजिए — बुरहानपुर के आगे घाट पे तेज़ बारिश आ रही है, विज़िबिलिटी कम है। "
                     "स्पीड चालीस रखिए, आगे वाले से दूरी डबल। तिरपाल ठीक से बंधा है ना? मार्बल भीगे तो दिक्कत नहीं, पर फिसलन है।",
                     card="🌧️ Heavy rain ahead (Burhanpur ghat, demo): 40 km/h, double distance")
    await ceo_tell("🌧️ Heavy rain on Burhanpur ghat (demo). Driver told to slow to 40 km/h. Watching speed.")
    await drive_to(km_of("Burhanpur") + 8, 8, speed=40)
    S["weather"] = "clear"


async def s_rest():
    v = hero()
    db.ex("UPDATE vehicles SET driving_since=? WHERE id=?",
          ((datetime.now(timezone.utc) - timedelta(hours=4, minutes=40)).isoformat(timespec="seconds"), v["id"]))
    await drive_to(km_of("Malkapur"), 6)
    await stop_here()
    expense("diesel", 9660, "Demo Highway Dhaba & Fuel, Malkapur (demo)", litres=105)
    note("Rest: 4 h 40 min driving → 30 min chai break + 2nd diesel (105 L) at Demo Highway Dhaba, Malkapur")
    await driver_say("चार घंटे चालीस मिनट हो गए लगातार चलाते हुए। यहीं Demo Highway Dhaba पे आधे घंटे रुकिए — "
                     "चाय, थोड़ा खाना, मुँह धो लीजिए। साथ में यहीं 105 लीटर डीज़ल भी डलवा लिया, नौ हज़ार छह सौ साठ — लिख लिया। मैं अलार्म लगा देता हूँ। वैसे बच्चों से बात हुई आज? एक फ़ोन कर लीजिए।")
    await ceo_tell("☕ MW-T1 resting 30 min at Malkapur (4 h 40 m driving). Back on road after the break.")


async def s_breakdown():
    from .agents import driver as drv
    v = hero()
    db.ex("UPDATE vehicles SET driving_since=NULL WHERE id=?", (v["id"],))
    await drive_to(km_of("Akola") - 15, 8)
    await stop_here()
    note("EMERGENCY: rear tyre burst 15 km before Akola → breakdown flow (real tools)")
    await driver_say("धमाका सुना! लगता है पीछे का टायर फटा है। गाड़ी धीरे-धीरे बाएं किनारे लगाइए, हैज़र्ड लाइट जलाइए। "
                     "घबराइए मत, मैं सब संभाल रहा हूँ।", card="🛞 Tyre burst — pull over left, hazards on")
    await asyncio.sleep(0 if S.get("fast") else 4)
    tools = {t.name: t for t in drv.make_tools(v["id"], drv.Turn("पीछे का टायर फट गया"))}
    res = await tools["report_breakdown"].fn(problem="rear tyre burst (demo), truck loaded 18 t", severity="high")
    S["mesh_id"] = (res.get("peer_help") or {}).get("request_id")
    if res.get("say"):
        await driver_say(res["say"])


async def s_mesh_help():
    from .agents import mesh
    helper = db.vehicle_by_code(HELPER)
    hv = hero()
    # helper truck (other fleet) is a few km away near Akola
    await fatigue.location_update(helper["id"], hv["last_lat"] + 0.035, hv["last_lon"] + 0.02, 0)
    rid = S.get("mesh_id")
    if not rid:
        r = await mesh.request_help(db.vehicle_by_code(HERO), "rear tyre burst (demo)")
        rid = r.get("request_id")
    note(f"Agent-to-agent: {HELPER}'s Saarthi (other fleet, demo driver Suresh) gets the request and accepts")
    if rid:
        reply = await mesh.respond(db.vehicle_by_code(HELPER), rid, True)
        db.log_agent(f"driver:{HELPER}", "mesh", "helper_reply", {"text": reply})
    await driver_say("अच्छी ख़बर! श्री गणेश ट्रांसपोर्ट का ट्रक, सुरेश भाई, चार किलोमीटर पे है — उनके पास जैक और "
                     "स्टेपनी खोलने का औज़ार है, आ रहे हैं। और Akola Demo Tyre Works का मिस्त्री भी नया टायर लेकर निकल गया है, "
                     "पच्चीस मिनट में पहुँचेगा। तब तक पीछे तिकोना लगा दीजिए।",
                     card="🤝 Help coming: SG-T1 (Suresh, 4 km) + Akola Demo Tyre Works (25 min)")
    await ceo_tell("🤝 Fleet Mesh: SG-T1 (Shree Ganesh Transport, demo) is 4 km away and coming to help MW-T1. "
                   "Tyre shop dispatched. I approved tyre budget up to ₹4,500.",
                   speak="मालिक, टायर फटा था, पर पास वाले ट्रक का ड्राइवर मदद को आ रहा है और मिस्त्री भी निकल गया है। "
                         "मैंने टायर के लिए साढ़े चार हज़ार तक की मंज़ूरी दे दी है।")


async def s_repaired():
    from .agents import driver as drv
    v = hero()
    expense("repair", 3800, "rear tyre replaced, Akola Demo Tyre Works (demo)")
    tools = {t.name: t for t in drv.make_tools(v["id"], drv.Turn("टायर बदल गया"))}
    await tools["problem_solved"].fn()
    note("Repaired in 48 min · ₹3,800 logged · mesh request closed · back on road")
    await driver_say("टायर बदल गया — तीन हज़ार आठ सौ, हिसाब में लिख दिया। सुरेश भाई को धन्यवाद बोल दिया उनके सारथी से। "
                     f"अड़तालीस मिनट गए, पर रात के प्लान में सब कवर हो जाएगा। नया ETA कल {eta_text(0.8)}।")
    await drive_to(km_of("Akola") + 15, 6)


async def s_papers():
    await drive_to(km_of("Nanded") - 10, 10)
    note("Papers: PUC expires in 4 days → CEO books renewal; police check at state border: documents shown on phone")
    await ceo_tell("📄 MW-T1 PUC expires in 4 days. I booked a renewal slot (demo) at Hyderabad for after unloading. "
                   "Insurance valid 200 days, permit valid 120 days, e-way bill valid 5 days.",
                   speak="मालिक, एक काम कर दिया — गाड़ी की PUC चार दिन में ख़त्म हो रही थी, हैदराबाद में रिन्यूअल बुक कर दिया।")
    await driver_say("एक बात — गाड़ी की PUC चार दिन में ख़त्म हो रही है, मालिक ने हैदराबाद में रिन्यूअल बुक करवा दिया है। "
                     "आगे तेलंगाना बॉर्डर पे चेकिंग हो सकती है — सब कागज़ आपकी स्क्रीन पे हैं: RC, इंश्योरेंस, परमिट, "
                     "ई-वे बिल। बस फ़ोन दिखा दीजिए।",
                     card="📄 Documents (demo): RC ✓ · Insurance ✓ 200 d · National Permit ✓ 120 d · PUC ⚠ 4 d (renewal booked) · "
                          "E-way bill EWB-DEMO-TS-4401 ✓")


async def s_client_call():
    await drive_to(km_of("Bhainsa"), 8)
    note("Telangana entered · client asked ETA → Saarthi answered & office updated; owner offers return load")
    await driver_say("तेलंगाना में स्वागत है! क्लाइंट Deccan Stone का फ़ोन आया था ETA पूछने — मैंने मैसेज भेज दिया: "
                     f"कल {eta_text()} तक पहुँचेंगे, अनलोडिंग के लिए चार मज़दूर तैयार रखें। आप बस आराम से चलाइए।")
    await ceo_tell(f"📞 Client {CLIENT} asked for ETA — replied automatically: {eta_text()}. "
                   "🔁 Return load found: Hyderabad → Udaipur, granite 16 t, ₹64,000 (demo, Shree Ganesh Transport). "
                   "Say 'accept' to book it.",
                   speak="मालिक, वापसी का लोड भी मिल गया — हैदराबाद से उदयपुर, चौंसठ हज़ार। बोलें तो बुक कर दूँ?")
    shree = db.one("SELECT * FROM fleets WHERE code='SHREE'")
    if shree and not db.one("SELECT id FROM loads WHERE origin='Hyderabad' AND status='open'"):
        db.ex("INSERT INTO loads(fleet_id,origin,destination,o_lat,o_lon,d_lat,d_lon,weight_t,vtype,freight,status,created_at) "
              "VALUES(?, 'Hyderabad', 'Udaipur', ?, ?, 24.585, 73.712, 16, 'truck_10w', 64000, 'open', ?)",
              (shree["id"], DEST[0], DEST[1], db.now()))


async def s_arrive():
    from . import business
    await drive_to(total_km(), 12)
    await stop_here()
    v = hero()
    await business.mark_site(v, "arrived", "unloading: Hyderabad (demo warehouse)")
    res = await business.delivered(v, "Venkat (demo), Deccan Stone Traders")
    m = money()
    S["final"] = m
    note(f"Delivered at Hyderabad · POD requested · trip spend {inr(m['spent'])} · gross margin {inr(m['profit'])}")
    await driver_say("पहुँच गए हैदराबाद! शाबाश रमेश भाई — बारिश, जाम, टायर, सब पार करके माल सही-सलामत पहुँचाया। "
                     "अनलोडिंग के बाद बिल्टी की फोटो खींच लीजिए, स्क्रीन पे बटन है — उससे पैसा जल्दी आएगा। "
                     "और हाँ, आज रात अच्छा खाना खाइए, आपने कमाया है!", card="📦 Delivered — take POD photo")
    await ceo_tell(f"📦 Delivered (demo): Udaipur → Hyderabad, {total_km():.0f} km. Freight {inr(FREIGHT)}, advance {inr(ADVANCE)}, "
                   f"balance due {inr(FREIGHT - ADVANCE)}. Spend: diesel {inr(m.get('diesel', 0))}, tolls {inr(m.get('toll', 0))} "
                   f"({m.get('toll_count', 0)} plazas), repair {inr(m.get('repair', 0))}. Trip margin before driver pay: {inr(m['profit'])}. "
                   f"Invoice ready once POD photo arrives.",
                   speak=f"मालिक, गाड़ी हैदराबाद पहुँच गई, माल डिलीवर हो गया। कुल ख़र्चा {inr(m['spent'])}, "
                         f"इस ट्रिप में {inr(m['profit'])} बचे। बिल्टी की फोटो आते ही बिल भेज दूँगा।")
    return res


STEPS = [("Trip assigned (CEO → driver)", s_setup), ("Route plan: tolls, diesel, rests, ETA", s_brief),
         ("Depart · live location · FASTag tolls", s_depart), ("Fuel stop (driver says diesel by voice)", s_fuel),
         ("Diesel logged → CEO", s_fuel_done), ("Traffic jam → reroute", s_traffic), ("Heavy rain warning", s_weather),
         ("Fatigue → rest at dhaba", s_rest), ("EMERGENCY: tyre burst", s_breakdown),
         ("Agent-to-agent help (other fleet)", s_mesh_help), ("Repaired, back on road", s_repaired),
         ("Papers + border check", s_papers), ("Client ETA call + return load", s_client_call),
         ("Arrive Hyderabad · delivery · POD · P&L", s_arrive)]


async def next_step() -> dict:
    if S["running"]:
        return state()
    i = S["step"] + 1
    if i >= len(STEPS):
        return state()
    S["running"] = True
    try:
        await STEPS[i][1]()
        S["step"] = i
    except Exception as e:
        log.exception("scenario step failed")
        note(f"⚠️ step '{STEPS[i][0]}' failed: {e}")
    finally:
        S["running"] = False
    return state()


async def _auto(gap: float) -> None:
    while S["auto"] and S["step"] + 1 < len(STEPS):
        await next_step()
        await asyncio.sleep(gap)
    S["auto"] = False


def start_auto(gap: float = 30.0) -> dict:
    if not S["auto"]:
        S["auto"] = True
        S["task"] = asyncio.create_task(_auto(gap))
    return state()


def stop_auto() -> dict:
    S["auto"] = False
    return state()


async def restart() -> dict:
    S.update(step=-1, auto=False, log=[], km=0.0)
    return await next_step()


def state() -> dict:
    return {"step": S["step"], "running": S["running"], "auto": S["auto"], "km": round(S["km"]),
            "route": [[round(a, 4), round(b, 4)] for a, b in _route[::3]] if _route else [],
            "total_km": round(total_km()), "steps": [s[0] for s in STEPS], "log": S["log"][-25:],
            "plan": S.get("plan"), "final": S.get("final")}
