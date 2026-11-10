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
