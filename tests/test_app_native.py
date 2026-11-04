"""Driver app (Android) <-> server: calls answered by voice, messages read aloud, native GPS, FASTag SMS."""
from aiohttp.test_utils import TestClient, TestServer
from pipecat.frames.frames import InputTransportMessageFrame, TranscriptionFrame, TTSSpeakFrame
from pipecat.tests.utils import run_test

from saarthi import db
from saarthi.bus import bus
from saarthi.companion import CompanionGuard, LiveSession
from saarthi.web import build_app


def mw1():
    return db.vehicle_by_code("MW-T1")


def guard():
    s = LiveSession(mw1(), None)
    s.last_bot_speech = 0
    return CompanionGuard(s)


def tf(text):
    return TranscriptionFrame(text=text, user_id="d", timestamp="0")


async def test_incoming_call_answered_by_voice(fresh):
    bus.sent.clear()
    await run_test(guard(), frames_to_send=[InputTransportMessageFrame(message={"type": "incoming_call", "name": "Sharma ji"})])
    assert any("Sharma ji ka call aa raha hai" in (o.speak or "") for o in bus.sent)
    bus.sent.clear()
    down, _ = await run_test(guard(), frames_to_send=[tf("haan utha lo")], expected_down_frames=[TTSSpeakFrame])
    assert "speaker" in down[0].text
    assert any(a == {"type": "answer_call"} for o in bus.sent for a in o.actions)


async def test_incoming_call_rejected(fresh):
    await run_test(guard(), frames_to_send=[InputTransportMessageFrame(message={"type": "incoming_call", "name": None})])
    bus.sent.clear()
    await run_test(guard(), frames_to_send=[tf("nahi")], expected_down_frames=[TTSSpeakFrame])
    assert any(a == {"type": "end_call"} for o in bus.sent for a in o.actions)


async def test_message_read_aloud_short_when_moving(fresh):
    db.ex("UPDATE vehicles SET last_speed_kmph=50, last_seen=? WHERE id=?", (db.now(), mw1()["id"]))
    bus.sent.clear()
    long_text = "Beta khana kha liya? " * 10
    await run_test(guard(), frames_to_send=[InputTransportMessageFrame(message={
        "type": "notification", "app": "WhatsApp", "title": "Papa", "text": long_text})])
    spoken = next(o.speak for o in bus.sent if o.speak and "Papa ka WhatsApp" in o.speak)
    assert "gaadi rukne pe" in spoken and len(spoken) < 160


async def test_native_gps_and_fastag_sms_with_truck_key(fresh):
    v = mw1()
    async with TestClient(TestServer(build_app())) as c:
        r = await c.post(f"/api/location?v=MW-T1&k={v['access_token']}", json={"lat": 24.30, "lon": 73.68, "speed": 13.9})
        assert (await r.json())["ok"]
        assert db.vehicle(v["id"])["last_lat"] == 24.30 and round(db.vehicle(v["id"])["last_speed_kmph"]) == 50
        assert (await c.post("/api/location?v=MW-T1&k=wrong", json={"lat": 1, "lon": 1})).status == 401
        await c.post(f"/api/location?v=MW-T1&k={v['access_token']}", json={"lat": 24.2714, "lon": 73.6738, "speed": 13})
        await c.post(f"/api/location?v=MW-T1&k={v['access_token']}", json={"lat": 24.24, "lon": 73.67, "speed": 13})
        r = await c.post(f"/api/fastag/sms?v=MW-T1&k={v['access_token']}",
                         json={"text": "Rs.430.00 debited from FASTag at PADUNA toll plaza."})
        assert (await r.json())["status"] == "matched"
