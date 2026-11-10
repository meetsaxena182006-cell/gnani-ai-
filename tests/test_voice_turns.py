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
