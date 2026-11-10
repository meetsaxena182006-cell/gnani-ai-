"""Live review over the web: driver typed chat, inboxes, permanent links, demo truck GPS guard."""
from aiohttp.test_utils import TestClient, TestServer

from saarthi import db, demo_drive, llm, seed
from saarthi.bus import bus, to_driver
from saarthi.config import settings
from saarthi.web import build_app, record_message
from tests.conftest import FakeLLM


def test_permanent_driver_tokens(monkeypatch):
    monkeypatch.setenv("LINK_SECRET", "abc")
    assert seed.access_token("MW-T1") == seed.access_token("MW-T1")
    assert seed.access_token("MW-T1") != seed.access_token("MW-T2")
    monkeypatch.delenv("LINK_SECRET")
    assert seed.access_token("MW-T1") != seed.access_token("MW-T1")   # random without the secret


async def test_driver_chat_reaches_agent_and_both_inboxes(fresh):
    bus.set_recorder(record_message)
    settings.api_shared_secret = "s3cret"
    llm.set_client(FakeLLM(["Ram Ram Ramesh bhai! Bore mat hoiye, ek kissa sunata hoon."]))
    v = db.vehicle_by_code("MW-T1")
    q = f"v={v['code']}&k={v['access_token']}"
    try:
        async with TestClient(TestServer(build_app())) as c:
            assert (await c.post("/api/driver/chat?v=MW-T1&k=wrong", json={"text": "hi"})).status == 401
            r = await (await c.post(f"/api/driver/chat?{q}", json={"text": "bore ho raha hoon"})).json()
            assert "Ram Ram" in r["reply"]
            feed = await (await c.get(f"/api/driver/feed?{q}")).json()
            kinds = [(x["kind"], x["payload"]["text"]) for x in feed["rows"]]
            assert kinds[0] == ("driver_said", "bore ho raha hoon") and "Ram Ram" in kinds[-1][1]
            # the CEO asks the driver something: it shows on his page and in the office
            await bus.send(to_driver(v, "Driver sahab, Paduna toll ka kitna laga?"))
            feed2 = await (await c.get(f"/api/driver/feed?{q}&since={feed['rows'][-1]['id']}")).json()
            assert feed2["rows"][0]["payload"]["text"].startswith("Driver sahab")
            assert (await c.get("/api/office/feed?fleet=MEWAR")).status == 401
            off = await (await c.get("/api/office/feed?fleet=MEWAR", headers={"X-Saarthi-Key": "s3cret"})).json()
            texts = [x["payload"]["text"] for x in off["rows"]]
            assert "bore ho raha hoon" in texts and any("Paduna" in t for t in texts)
            # links page: office + every driver
            assert (await c.get("/links?k=bad")).status == 401
            page = await (await c.get("/links?k=s3cret")).text()
            assert "/office?k=s3cret" in page and f"/driver?v=MW-T1&amp;k={v['access_token']}" in page
    finally:
        bus.set_recorder(None)


async def test_owner_alert_goes_to_owner_inbox(fresh):
    from saarthi.agents import ceo
    bus.set_recorder(record_message)
    try:
        await bus.send(ceo.owner_out(db.fleet(db.vehicle_by_code("MW-T1")["fleet_id"]), "⚠️ PUC expired"))
        row = db.one("SELECT * FROM agent_log WHERE kind='message' ORDER BY id DESC LIMIT 1")
        assert row["to_agent"] == "owner:MEWAR"
    finally:
        bus.set_recorder(None)


async def test_demo_truck_ignores_phone_gps(fresh):
    v = db.vehicle_by_code("MW-T3")
    demo_drive.SIM_IDS.add(v["id"])
    try:
        async with TestClient(TestServer(build_app())) as c:
            r = await (await c.post(f"/api/location?v=MW-T3&k={v['access_token']}", json={"lat": 19.0, "lon": 72.8})).json()
            assert r.get("ignored")
            assert db.vehicle(v["id"])["last_lat"] != 19.0
    finally:
        demo_drive.SIM_IDS.discard(v["id"])
