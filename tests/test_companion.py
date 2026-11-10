"""Live companion guard: deterministic handling before the LLM, using Pipecat's own test harness."""
import time

from pipecat.frames.frames import InputTransportMessageFrame, TranscriptionFrame, TTSSpeakFrame
from pipecat.tests.utils import run_test

from saarthi import db
from saarthi.agents import mesh
from saarthi.bus import bus
from saarthi.companion import CompanionGuard, LiveSession, addressed
from saarthi.state import ask


def tf(text):
    return TranscriptionFrame(text=text, user_id="driver", timestamp="0")


def session(code="MW-T1"):
    s = LiveSession(db.vehicle_by_code(code), connection=None)
    s.last_bot_speech = 0  # conversation closed
    return s


def test_addressing_rules():
    assert addressed("saarthi diesel 4500 ka daala", 0, "wake")
    assert addressed("सारथी सुनो", 0, "wake")
    assert addressed("bhai tyre phat gaya", 0, "wake")          # breakdown always gets through
    assert not addressed("haan bhai main pahunch raha hoon ghar", 0, "wake")  # talking to family on the phone
    assert addressed("haan bhai", time.time(), "wake")           # open conversation


async def test_background_talk_is_not_sent_to_llm(fresh, monkeypatch):
    from saarthi import companion
    monkeypatch.setattr(companion, "ADDRESS_MODE", "wake")   # passengers in the cab: only answer when addressed
    down, _ = await run_test(CompanionGuard(session()), frames_to_send=[tf("mummy ko bolo khana bana le")],
                             expected_down_frames=[])
    assert not down


async def test_addressed_speech_goes_to_llm(fresh):
    s = session()
    await run_test(CompanionGuard(s), frames_to_send=[tf("Saarthi, mausam kaisa hai aage?")],
                   expected_down_frames=[TranscriptionFrame])
    assert s.turn.transcript.startswith("Saarthi")


async def test_yes_confirms_money_in_code(fresh):
    v = db.vehicle_by_code("MW-T1")
    db.ex("INSERT INTO expenses(vehicle_id,category,amount,litres,status,created_at) VALUES(?,?,?,?, 'pending', ?)",
          (v["id"], "diesel", 4500, 48, db.now()))
    down, _ = await run_test(CompanionGuard(session()), frames_to_send=[tf("haan")],
                             expected_down_frames=[TTSSpeakFrame])
    assert "Save ho gaya" in down[0].text
    assert db.one("SELECT status FROM expenses")["status"] == "confirmed"


async def test_helper_says_yes_by_voice_and_others_are_told(fresh):
    requester = db.vehicle_by_code("MW-T1")
    res = await mesh.request_help(requester, "tyre phat gaya")
    assert res["status"] == "asked"
    bus.sent.clear()
    down, _ = await run_test(CompanionGuard(session("SG-T1")), frames_to_send=[tf("haan ji")],
                             expected_down_frames=[TTSSpeakFrame])
    assert "hero" in down[0].text
    texts = [(o.vehicle_id, o.speak or "") for o in bus.sent]
    assert any(vid == requester["id"] and "madad" in s.lower() for vid, s in texts)       # requester told
    mw2 = db.vehicle_by_code("MW-T2")["id"]
    assert any(vid == mw2 and "madad mil gayi" in s for vid, s in texts)                   # others told


async def test_dial_only_after_yes(fresh):
    v = db.vehicle_by_code("MW-T1")
    ask(v["id"], "dial", name="Demo Tyre Works", phone="+910000000000")
    bus.sent.clear()
    await run_test(CompanionGuard(session()), frames_to_send=[tf("haan")], expected_down_frames=[TTSSpeakFrame])
    assert any(a.get("type") == "dial" and a["phone"] == "+910000000000" for o in bus.sent for a in o.actions)


async def test_gps_from_phone_updates_location(fresh):
    await run_test(CompanionGuard(session()),
                   frames_to_send=[InputTransportMessageFrame(message={"type": "location", "lat": 24.6, "lon": 73.8, "speed": 15})])
    v = db.vehicle_by_code("MW-T1")
    assert v["last_lat"] == 24.6 and round(v["last_speed_kmph"]) == 54


def test_always_mode_answers_everything():
    assert addressed("haan bhai main pahunch raha hoon ghar", 0, "always")
