"""Trip & fleet planner: legal/health breaks, weather, fuel, no-entry, departure optimisation, optimal assignment."""
import itertools
import random
from datetime import datetime, timedelta, timezone

from saarthi import db, geo, planner
from saarthi.agents.common import IST
from saarthi.planner import TripRequest, Weather, simulate

LONG = [(24.0, 73.0 + 0.1 * i) for i in range(61)]          # ~610 km straight east-west line
START = datetime(2026, 11, 5, 6, 0, tzinfo=IST)


def fog_js(start: datetime, fog_hours=(), days=3):
    times, codes, vis = [], [], []
    t = start.astimezone(IST).replace(minute=0) - timedelta(hours=start.astimezone(IST).hour)
    for h in range(24 * days):
        cur = t + timedelta(hours=h)
        times.append(cur.strftime("%Y-%m-%dT%H:00"))
        f = cur.hour in fog_hours
        codes.append(45 if f else 0); vis.append(300 if f else 20000)
    z = [0] * len(times)
    return {"hourly": {"time": times, "weather_code": codes, "visibility": vis, "precipitation": z,
                       "precipitation_probability": z, "temperature_2m": [25] * len(times), "wind_speed_10m": [10] * len(times)}}


def sim(req, dep=START, weather=None, rules=None, km=610, base_h=13.0):
    return simulate(req, 0, LONG, km, base_h, dep, weather or Weather([]), rules or [])


def test_breaks_follow_law_and_health():
    s = sim(TripRequest(origin=LONG[0], destination=LONG[-1]))
    drive_since_stop, drive_since_rest, duty_drive = 0.0, 0.0, 0.0
    # rebuild driving between stops from km positions (constant speed, no weather)
    speed = 610 / 13.0
    last_km = 0
    for st in s.stops:
        stretch = (st["at_km"] - last_km) / speed
        drive_since_stop = stretch
        drive_since_rest += stretch; duty_drive += stretch
        assert drive_since_stop <= planner.HEALTH_BREAK_H + 0.15          # a break at least every ~2 h
        assert drive_since_rest <= planner.LEGAL_REST_AFTER_H + 0.01       # legal rest within 5 h
        assert duty_drive <= planner.DAILY_DRIVE_H + 0.15                  # 8 h driving per duty day
        if st["kind"].startswith("meal_rest"):
            assert st["minutes"] >= 30
            drive_since_rest = 0
        if st["kind"] == "sleep":
            assert st["minutes"] == 480
            drive_since_rest = duty_drive = 0
        last_km = st["at_km"]
    assert any(st["kind"] == "sleep" for st in s.stops)                    # 13 h of driving needs a night halt
    assert 13.0 - 0.05 <= s.driving_h <= 13.0 / 0.95 + 0.05   # night stretches are driven 5% slower


def test_relief_driver_skips_night_halt():
    s = sim(TripRequest(origin=LONG[0], destination=LONG[-1], relief_driver=True))
    assert not any(st["kind"] == "sleep" for st in s.stops)


def test_fog_slows_and_is_counted():
    w = Weather([(0, fog_js(START, fog_hours=range(6, 12))), (610, fog_js(START, fog_hours=range(6, 12)))])
    clear = sim(TripRequest(origin=LONG[0], destination=LONG[-1], relief_driver=True))
    foggy = sim(TripRequest(origin=LONG[0], destination=LONG[-1], relief_driver=True), weather=w)
    assert foggy.hazard_h.get("fog", 0) > 3
    assert foggy.arrive > clear.arrive


def test_refuel_planned_when_tank_low():
    s = sim(TripRequest(origin=LONG[0], destination=LONG[-1], fuel_now_l=80, relief_driver=True))
    assert s.refuels >= 1 and any("refuel" in st["kind"] for st in s.stops)


def test_no_entry_window_adds_wait():
    rules = [{"city": "Demo City", "lat": LONG[-1][0], "lon": LONG[-1][1], "radius_km": 10, "no_entry": [["00:00", "23:59"]]}]
    s = sim(TripRequest(origin=LONG[0], destination=LONG[-1], relief_driver=True), rules=rules)
    assert s.no_entry_wait_h > 0 and s.stops[-1]["kind"] == "no_entry_wait"


async def test_planner_avoids_night_fog_and_names_stops(monkeypatch):
    now = datetime.now(timezone.utc)
    night_fog = fog_js(now, fog_hours=range(0, 7))

    async def route(*a):
        return {"source": "test", "traffic": "not available",
                "routes": [{"distance_km": 610.0, "duration_h": 9.3, "truck_duration_h": 13.0, "_coords": LONG}]}

    async def hourly(lat, lon, days=3):
        return night_fog

    async def places(points, kinds=("fuel", "dhaba"), radius_m=3000):
        return [[{"name": f"Demo Dhaba {i}", "distance_km": 0.4}] for i, _ in enumerate(points)]

    monkeypatch.setattr(geo, "route", route)
    monkeypatch.setattr(geo, "forecast_hourly", hourly)
    monkeypatch.setattr(geo, "overpass_along", places)
    req = TripRequest(origin=LONG[0], destination=LONG[-1], freight=60000, relief_driver=True,
                      depart_after=now.replace(hour=16, minute=30) if now.hour < 16 else now)
    plan = await planner.plan_trip(req)
    best, leave_now = plan, plan["alternatives"]["leave_now"]
    assert best["night_driving_h"] <= leave_now["night_driving_h"]
    assert best["weather_risk_h"].get("fog", 0) <= leave_now["fog_h"]
    assert all("Demo Dhaba" in s.get("place", "") for s in best["stops"] if s["kind"] != "no_entry_wait")
    assert best["profit"] is not None and best["why"]
    text = planner.owner_summary(best, "MW-T1 test")
    assert "Depart" in text and "km" in text and "Why:" in text
    km_mid = planner.expected_km_at(best, datetime.fromisoformat(best["depart_iso"]) + timedelta(hours=3))
    assert 0 < km_mid < 610


def test_late_arrival_is_penalised():
    req = TripRequest(origin=LONG[0], destination=LONG[-1], freight=60000, relief_driver=True,
                      latest_arrival=START + timedelta(hours=12))
    early = sim(req, dep=START)
    late = sim(req, dep=START + timedelta(hours=6))
    assert planner.score(req, early, 610, 0)["score"] > planner.score(req, late, 610, 0)["score"]


def test_hungarian_is_optimal():
    rnd = random.Random(7)
    for _ in range(30):
        n_r, n_c = rnd.randint(1, 5), rnd.randint(1, 5)
        m = [[rnd.randint(-50, 100) for _ in range(n_c)] for _ in range(n_r)]
        got = sum(m[i][j] for i, j in planner.hungarian_max(m))
        best = max(sum(m[i][p[i]] for i in range(n_r) if p[i] is not None)
                   for p in itertools.permutations(list(range(n_c)) + [None] * n_r, n_r))
        assert got == best


def test_fleet_plan_assigns_and_consolidates(fresh):
    shree = db.one("SELECT * FROM fleets WHERE code='SHREE'")
    mewar = db.one("SELECT * FROM fleets WHERE code='MEWAR'")
    def add(fid, o, d, freight, w):
        return db.ex("INSERT INTO loads(fleet_id,origin,destination,o_lat,o_lon,d_lat,d_lon,weight_t,freight,status,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?, 'open', ?)", (fid, o[2], d[2], o[0], o[1], d[0], d[1], w, freight, db.now()))
    UDR, AMD, JPR = (24.585, 73.712, "Udaipur"), (23.0225, 72.5714, "Ahmedabad"), (26.912, 75.787, "Jaipur")
    l1 = add(shree["id"], AMD, UDR, 17000, 9)       # return load for MW-T1 (reaching Ahmedabad)
    l2 = add(mewar["id"], UDR, JPR, 24000, 8)       # for MW-T2 near Udaipur
    l3 = add(shree["id"], AMD, UDR, 9000, 5)        # part load -> consolidate with l1 (9 + 5 <= 16 t)
    l4 = add(mewar["id"], UDR, JPR, 30000, 25)      # too heavy for the 6-wheeler / 10-wheeler
    res = planner.plan_fleet(mewar["id"])
    by_vehicle = {a["vehicle"]: a for a in res["assignments"]}
    assert by_vehicle["MW-T1"]["load_id"] == l1
    assert by_vehicle["MW-T2"]["load_id"] == l2
    assert any(x["load_id"] == l3 for x in by_vehicle["MW-T1"].get("add_on_loads", []))
    assert {"load_id": l4, "load": "Udaipur → Jaipur"} in res["unassigned_loads"]
    assert res["fleet_km_utilisation_pct"] > 50


NH48 = [(24.505, 73.73), (24.40, 73.70), (24.2714, 73.6738), (23.9936, 73.6279), (23.62, 73.255), (23.0225, 72.5714)]


def patch_geo(monkeypatch, fog=()):
    async def route(*a):
        return {"source": "test", "traffic": "not available",
                "routes": [{"distance_km": 262.0, "duration_h": 4.6, "truck_duration_h": 6.4, "_coords": NH48}]}

    async def hourly(lat, lon, days=3):
        return fog_js(datetime.now(timezone.utc), fog_hours=fog)

    async def places(points, kinds=("fuel", "dhaba"), radius_m=3000):
        return [[{"name": "Demo Highway Dhaba", "distance_km": 0.3}] for _ in points]
    monkeypatch.setattr(geo, "route", route)
    monkeypatch.setattr(geo, "forecast_hourly", hourly)
    monkeypatch.setattr(geo, "overpass_along", places)


async def test_plan_for_truck_updates_ceo_and_driver(fresh, monkeypatch):
    from saarthi.bus import bus
    patch_geo(monkeypatch)
    v = db.vehicle_by_code("MW-T1")
    bus.sent.clear()
    plan = await planner.plan_for_vehicle(v)
    assert plan["route"]["toll_plazas"] and plan["route"]["tolls_estimate"] > 0      # NHAI plazas on NH48
    assert plan["stops"] and plan["profit"] is not None
    assert any(o.chat_id == 1001 and "Trip plan" in o.text and "Demo Highway Dhaba" in o.text for o in bus.sent)
    assert any(o.vehicle_id == v["id"] and "Plan taiyaar" in (o.speak or "") for o in bus.sent)
    assert planner.latest_plan(db.active_trip(v["id"])["id"])["arrive"] == plan["arrive"]


async def test_monitor_reminds_stops_and_reports_delay(fresh, monkeypatch):
    from saarthi.bus import bus
    patch_geo(monkeypatch)
    v = db.vehicle_by_code("MW-T1")
    t = db.active_trip(v["id"])
    plan = await planner.plan_for_vehicle(v, notify=False)
    first = plan["stops"][0]
    # truck is 5 km before the first planned stop -> reminder
    db.ex("UPDATE trips SET gps_km=? WHERE id=?", (first["at_km"] - 5, t["id"]))
    bus.sent.clear()
    await planner.monitor()
    assert any(o.vehicle_id == v["id"] and "plan hai" in (o.speak or "") for o in bus.sent)
    # 3 hours after departure the truck has only done 10 km -> owner gets a new ETA, driver is not pushed
    import json
    p = planner.latest_plan(t["id"])
    # the planner chose a later (daylight) departure; pretend it departed 3 h ago
    shift = datetime.fromisoformat(p["depart_iso"]) - (datetime.now(timezone.utc) - timedelta(hours=3))
    p["depart_iso"] = (datetime.fromisoformat(p["depart_iso"]) - shift).isoformat()
    p["arrive_iso"] = (datetime.fromisoformat(p["arrive_iso"]) - shift).isoformat()
    db.ex("UPDATE trip_plans SET plan=? WHERE trip_id=?", (json.dumps(p), t["id"]))
    db.ex("UPDATE trips SET gps_km=10 WHERE id=?", (t["id"],))
    bus.sent.clear()
    await planner.monitor()
    assert any(o.chat_id == 1001 and "behind plan" in o.text and "New ETA" in o.text for o in bus.sent)
    assert not any(o.vehicle_id == v["id"] and "behind" in (o.speak or o.text) for o in bus.sent)


async def test_ceo_fleet_plan_tool(fresh):
    from saarthi.agents import ceo
    f = db.one("SELECT * FROM fleets WHERE code='MEWAR'")
    tools = {t.name: t for t in ceo._fleet_tools(f["id"])}
    assert "plan_trip" in tools and "plan_fleet" in tools
    res = await tools["plan_fleet"].fn()
    assert "assignments" in res
