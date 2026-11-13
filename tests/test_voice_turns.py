"""A driver who was heard always gets an answer: Gnani transcript -> guard -> user turn -> LLM request."""
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, LLMContextFrame,
                                   TranscriptionFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.tests.utils import SleepFrame, run_test

from saarthi import db
from saarthi.companion import CompanionGuard, LiveSession, _user_params


def _chain():
    s = LiveSession(db.vehicle_by_code("MW-T1"), connection=None)
    agg = LLMContextAggregatorPair(LLMContext(messages=[{"role": "system", "content": "x"}]),
                                   user_params=_user_params(SileroVADAnalyzer))
    return Pipeline([CompanionGuard(s), agg.user()])


def tf(t):
    return TranscriptionFrame(text=t, user_id="driver", timestamp="0")


async def test_plain_talk_reaches_llm(fresh):
    down, _ = await run_test(_chain(), frames_to_send=[tf("aage mausam kaisa hai"), SleepFrame(1.5)],
                             expected_down_frames=None, ignore_start=True)
    assert any(isinstance(f, LLMContextFrame) for f in down)


async def test_talk_over_saarthi_reaches_llm(fresh):
    down, _ = await run_test(_chain(), frames_to_send=[BotStartedSpeakingFrame(), SleepFrame(0.2),
                                                       tf("ruko ruko, toll 157 laga"), SleepFrame(0.2),
                                                       BotStoppedSpeakingFrame(), SleepFrame(1.5)],
                             expected_down_frames=None, ignore_start=True)
    assert any(isinstance(f, LLMContextFrame) for f in down)


def test_clean_args_drops_junk_keys():
    from types import SimpleNamespace
    from saarthi.companion import clean_args
    tool = SimpleNamespace(parameters={"type": "object", "properties": {"vehicle": {"type": "string"}}})
    assert clean_args(tool, {"": {}, "vehicle": "MW-T1"}) == {"vehicle": "MW-T1"}
    assert clean_args(SimpleNamespace(parameters={}), {"": {}}) == {}


def test_groq_sibling_model_added(monkeypatch):
    from saarthi import config
    monkeypatch.setenv("LLM2_API_KEY", "x")
    monkeypatch.setenv("LLM2_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("LLM2_MODEL", "openai/gpt-oss-120b")
    models = [p["model"] for p in config.llm_providers() if "groq" in p["base_url"]]
    assert models[:2] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]


def test_rate_limited_model_steps_aside_for_failover():
    import asyncio
    from pipecat.pipeline.llm_switcher import LLMSwitcher
    from pipecat.pipeline.service_switcher import ServiceSwitcherStrategyFailover
    from pipecat.frames.frames import ErrorFrame
    from saarthi.companion import _voice_llm

    a = _voice_llm({"api_key": "x", "base_url": "https://api.groq.com/openai/v1", "model": "a"})
    b = _voice_llm({"api_key": "x", "base_url": "https://api.groq.com/openai/v1", "model": "b"})
    sw = LLMSwitcher(llms=[a, b], strategy_type=ServiceSwitcherStrategyFailover)

    async def go():
        err = ErrorFrame(error="Error during completion: Error code: 429 - rate limit")
        err.processor = a
        await a._call_event_handler("on_error", err)
        assert not a.is_usable
        new = await sw.strategy.handle_error(err)
        assert new is b
    asyncio.run(go())


def test_same_expense_said_once_is_queued_once(tmp_path, monkeypatch):
    import asyncio
    from saarthi import db, seed
    from saarthi.agents import driver as drv
    monkeypatch.setattr(db.settings, "db_path", str(tmp_path / "t.db"))
    db._conn = None
    seed.seed(demo=True)
    v = db.vehicle_by_code("MW-T1")
    tools = {t.name: t for t in drv.make_tools(v["id"], drv.Turn("diesel 235 litre 21620"))}
    async def go():
        await tools["log_entry"].fn(category="diesel", amount=21620, litres=235)
        await tools["log_entry"].fn(category="diesel", amount=21620, litres=235)
    asyncio.run(go())
    assert len(drv.pending_entries(v["id"])) == 1


def test_empty_truck_agent_books_safe_load_and_rejects_bad_ones(tmp_path, monkeypatch):
    import asyncio
    from saarthi import db, seed
    from saarthi.agents import backhaul
    monkeypatch.setattr(db.settings, "db_path", str(tmp_path / "b.db"))
    db._conn = None
    seed.seed(demo=False)
    v = db.vehicle_by_code("MW-T1")
    db.ex("UPDATE vehicles SET last_lat=17.44, last_lon=78.50 WHERE id=?", (v["id"],))
    shree = db.one("SELECT id FROM fleets WHERE code='SHREE'")["id"]
    db.ex("INSERT INTO fleets(code,name,verified,rating,overdue_payments) VALUES('QUICK','Quick (demo)',0,2.0,2)")
    quick = db.one("SELECT id FROM fleets WHERE code='QUICK'")["id"]
    ins = ("INSERT INTO loads(fleet_id,origin,destination,o_lat,o_lon,d_lat,d_lon,weight_t,vtype,freight,status,urgent,created_at)"
           " VALUES(?,?,?,?,?,?,?,?,?,?,'open',?,?)")
    db.ex(ins, (quick, "Hyderabad", "Jaipur", 17.40, 78.48, 26.91, 75.79, 14, "truck_10w", 98000, 0, db.now()))
    db.ex(ins, (shree, "Medchal", "Ahmedabad", 17.63, 78.48, 23.02, 72.57, 22, "truck_10w", 104000, 0, db.now()))
    db.ex(ins, (shree, "Medchal", "Udaipur", 17.63, 78.48, 24.585, 73.712, 14, "truck_10w", 72000, 1, db.now()))
    db.ex("UPDATE documents SET expiry_date='2099-01-01' WHERE vehicle_id=?", (v["id"],))
    res = asyncio.run(backhaul.match_empty(v, notify=False))
    assert res["booked"] and res["booked"]["route"] == "Medchal → Udaipur"
    bad = {c["route"]: c["rejected_because"] for c in res["checked"] if not c["ok"]}
    assert "poster not trustworthy" in bad["Hyderabad → Jaipur"]
    assert "overloading" in bad["Medchal → Ahmedabad"]
    assert db.active_trip(v["id"])["destination"] == "Udaipur"
