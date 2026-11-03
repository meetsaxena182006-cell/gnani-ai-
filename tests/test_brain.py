"""Companion brain: situation awareness, music, deep reasoning, road analysis with NHAI tolls."""
from pipecat.processors.aggregators.llm_context import LLMContext

from saarthi import db, geo, llm
from saarthi.agents import ceo, driver
from saarthi.bus import bus
from saarthi.companion import LiveSession
from tests.conftest import FakeLLM

# Udaipur -> Ahmedabad along NH48 (lat, lon), passing Paduna and Khandi Obari plazas
NH48 = [(24.505, 73.73), (24.40, 73.70), (24.2714, 73.6738), (23.9936, 73.6279), (23.62, 73.255), (23.0225, 72.5714)]


async def fake_route(o_lat, o_lon, d_lat, d_lon):
    return {"source": "test", "traffic": "not available", "routes": [{
        "distance_km": 260.0, "duration_h": 4.5, "truck_duration_h": 6.3,
        "sample_points": [NH48[0], NH48[2], NH48[4], NH48[5]], "_coords": NH48}]}


async def fake_forecast(lat, lon, hours):
    return {"at": "2026-11-03T22:00", "warnings": ["FOG: low visibility"] if lat < 23.7 else []}


def test_situation_knows_landmark_and_unreported_tolls(fresh):
    v = db.vehicle_by_code("MW-T1")
    t = db.active_trip(v["id"])
    db.ex("INSERT INTO toll_crossings(vehicle_id,trip_id,plaza,status,crossed_at) VALUES(?,?,?, 'unreported', ?)",
          (v["id"], t["id"], "Paduna", db.now()))
    s = driver.situation(v)
    assert "toll plaza" in s["landmark"] and s["unreported_tolls"] == ["Paduna"]
    assert "Paduna" in driver.system_prompt(v, False)


def test_live_context_refreshes_each_turn(fresh):
    v = db.vehicle_by_code("MW-T1")
    s = LiveSession(v, None)
    s.context = LLMContext(messages=[{"role": "system", "content": "old"}])
    db.ex("UPDATE vehicles SET last_speed_kmph=55 WHERE id=?", (v["id"],))
    s.refresh_context(db.vehicle(v["id"]))
    assert '"speed_kmph": 55' in s.context.get_messages()[0]["content"]


async def test_route_analysis_has_tolls_weather_eta(fresh, monkeypatch):
    monkeypatch.setattr(geo, "route", fake_route)
    monkeypatch.setattr(geo, "forecast_at", fake_forecast)
    v = db.vehicle_by_code("MW-T1")
    r = await driver.analyze_route(v, db.active_trip(v["id"]))
    opt = r["options"][0]
    assert opt["truck_eta_hours"] == 6.3 and opt["arrive_ist"]
    assert [p["name"] for p in opt["tolls"]["plazas"]][:2] == ["Paduna", "Khandi Obari"]
    assert opt["tolls"]["expected_total"] > 0
    assert any("FOG" in w for st in opt["weather_on_the_way"] for w in st["warnings"])


async def test_quote_uses_nhai_tolls_on_route(fresh, monkeypatch):
    monkeypatch.setattr(geo, "route", fake_route)
    async def fake_geocode(place):
        return {"name": place, "lat": 24.58, "lon": 73.71} if "Udaipur" in place else {"name": place, "lat": 23.02, "lon": 72.57}
    monkeypatch.setattr(geo, "geocode", fake_geocode)
    f = db.one("SELECT * FROM fleets WHERE code='MEWAR'")
    tools = {t.name: t for t in ceo._fleet_tools(f["id"])}
    q = await tools["quote_freight"].fn(origin="Udaipur", destination="Ahmedabad", vtype="truck_10w", offered_freight=30000)
    assert q["toll_plazas_on_route"]["plazas"] and q["cost_lines"]["tolls"] == q["toll_plazas_on_route"]["expected_total"]
    assert any("NHAI plazas" in w for w in q["warnings"])


async def test_music_and_deep_thinking(fresh, monkeypatch):
    monkeypatch.setenv("RADIO_STATIONS", '{"Demo Radio": "https://example.invalid/stream"}')
    v = db.vehicle_by_code("MW-T1")
    tools = {t.name: t for t in driver.make_tools(v["id"], driver.Turn())}
    bus.sent.clear()
    r = await tools["play_music"].fn(request="radio chalao", kind="radio")
    assert r["playing"] == "Demo Radio"
    assert any(a.get("type") == "music" and a["action"] == "play" for o in bus.sent for a in o.actions)
    r = await tools["play_music"].fn(request="Kishore Kumar ke gaane")
    assert any(a.get("type") == "open_url" and "Kishore" in a["url"] for o in bus.sent for a in o.actions)
    monkeypatch.setattr(geo, "route", fake_route)
    monkeypatch.setattr(geo, "forecast_at", fake_forecast)
    llm.set_client(FakeLLM(["Boss, 6 ghante lagenge, Vantada ke baad fog hai, raat 10 baje tak pahunchoge."]))
    r = await tools["think_deeply"].fn(question="aaj raat tak Ahmedabad pahunch jayenge?")
    assert "fog" in r["analysis"].lower()
    prompt = llm.get_client().calls[-1]["messages"][0]["content"]
    assert "Paduna" in prompt and "Never invent numbers" in prompt   # real data handed to the reasoning model
