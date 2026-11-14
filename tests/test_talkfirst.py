import asyncio

from saarthi import db, geo, talkfirst


async def _fake_weather(lat, lon):
    return {"now": "clear", "temp_c": 24.0, "wind_kmph": 5, "warnings": [], "source": "test"}


def _reset():
    talkfirst._drv.clear(); talkfirst._own.clear(); talkfirst._offers.clear()


async def test_driver_greeting_uses_real_state_and_ends_with_offer(fresh, monkeypatch):
    _reset()
    monkeypatch.setattr(geo, "weather", _fake_weather)
    v = db.one("SELECT * FROM vehicles WHERE code='MW-T2'")
    db.ex("UPDATE trips SET status='closed' WHERE vehicle_id=?", (v["id"],))
    db.ex("UPDATE vehicles SET last_lat=24.58, last_lon=73.71 WHERE id=?", (v["id"],))
    out = await talkfirst.driver_opener(dict(db.vehicle(v["id"])))
    assert out.startswith("राम राम Mahesh जी") and "24°C" in out and "लोड" in out
    # "haan" to that offer turns into the load search request
    assert talkfirst.take_offer(f"drv:{v['id']}", "haan") == "गाड़ी खाली है, अगला लोड ढूंढो"


async def test_driver_page_open_then_quiet_nudge(fresh, monkeypatch):
    _reset()
    monkeypatch.setattr(geo, "weather", _fake_weather)
    monkeypatch.setattr(talkfirst, "GAP_S", 0)
    v = dict(db.one("SELECT * FROM vehicles WHERE code='MW-T1'"))
    talkfirst.on_driver_poll(v, 0)
    await asyncio.sleep(0.3)
    talkfirst.on_driver_poll(v, 1)
    await asyncio.sleep(0.3)
    rows = db.q("SELECT payload FROM agent_log WHERE to_agent='driver:MW-T1' AND kind='message' AND payload LIKE '%proactive%'")
    assert len(rows) == 2
    talkfirst.on_driver_poll(v, 0)          # refresh within 2 min: no second greeting
    await asyncio.sleep(0.2)
    assert len(db.q("SELECT id FROM agent_log WHERE to_agent='driver:MW-T1' AND payload LIKE '%proactive%'")) == 2


def test_owner_briefing_and_nudge(fresh, monkeypatch):
    _reset()
    f = dict(db.one("SELECT * FROM fleets WHERE code='MEWAR'"))
    out = talkfirst.owner_poll(f, True)
    assert "CEO सारथी" in out and "अभी का हाल" in out
    assert talkfirst.owner_poll(f, False) is None          # just spoke: wait
    monkeypatch.setattr(talkfirst, "OWNER_GAP_S", 0)
    assert talkfirst.owner_poll(f, False)


def test_owner_call_is_simulated_and_grounded(fresh):
    f = dict(db.one("SELECT * FROM fleets WHERE code='MEWAR'"))
    out = talkfirst.owner_call(f, "MW-T1 ke driver ko call karo")
    assert "डेमो" in out and "Ramesh" in out
    assert talkfirst.owner_call(f, "is mahine ka munafa") is None


async def test_driver_call_owner_is_simulated(fresh):
    v = dict(db.one("SELECT * FROM vehicles WHERE code='MW-T1'"))
    out = await talkfirst.driver_call(v, "malik ko call lagao")
    assert "डेमो" in out
    assert await talkfirst.driver_call(v, "mausam kaisa hai") is None


def test_driver_relay_and_junk_place_names():
    import asyncio
    from saarthi import talkfirst
    from saarthi.geo import clean_place_name
    assert clean_place_name("uniadmin", "mechanic").startswith("Mechanic")
    assert clean_place_name("Contest Garage", "mechanic") == "Contest Garage"
    assert talkfirst._RELAY2.search("मालिक को बोलो डीज़ल के लिए 5000 भेज दें").group(6).startswith("डीज़ल")
    # breakdowns keep their own flow (alert with location + help), calls stay simulated calls
    assert asyncio.run(talkfirst.driver_relay({"fleet_id": 1}, "मालिक को बोलो टायर फट गया")) is None
    assert asyncio.run(talkfirst.driver_relay({"fleet_id": 1}, "मालिक को कॉल करो")) is None


def test_owner_relay_regex():
    from saarthi import talkfirst
    m = talkfirst._ORELAY.search("MW-T2 के ड्राइवर को बोलो अगले ढाबे पर खाना खा ले")
    assert m and m.group(6).startswith("अगले")
    assert talkfirst._ORELAY.search("किस गाड़ी के कागज़ ख़त्म हो रहे हैं?") is None
