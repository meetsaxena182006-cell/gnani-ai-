"""Live voice companion: an always-on, hands-free Driver Agent session per truck.

Phone (mic+speaker, locked on the dashboard)  <-- WebRTC -->  this pipeline:

  audio in -> [Gnani Prisma v3.0, VAD-segmented] -> CompanionGuard -> context -> LLM (tools) -> [Gnani Timbre] -> audio out

- VAD segments speech, so only speech is sent to Prisma (keeps the always-on session affordable).
- CompanionGuard (deterministic, before the LLM):
    * answers yes/no to the companion's own questions in code (confirm money, call mechanic/112, help a truck,
      alertness check);
    * stays quiet while the driver talks to someone else, unless addressed ("Saarthi ...") or in an open
      conversation; emergencies/breakdowns always get through;
    * follows the driver's language (Hindi, Punjabi, Gujarati, Marathi, ...) by switching the Timbre voice;
    * takes GPS updates from the phone over the data channel (no GPS hardware).
- Other agents reach the driver through `bus.live[vehicle_id].deliver(out)` (CEO instructions, Fleet Mesh asks,
  check-ins), which speaks the message and sends any action (e.g. dial a number) to the phone.
"""
from __future__ import annotations

import os
import asyncio
import logging
import re
import time

import aiohttp

from . import db, fatigue
from .agents import driver as drv
from .bus import LiveTarget, Out, bus
from .config import settings
from .safety import is_moving, rule_severity
from .speech import DEFAULT_TOPIC, load_keyterms

log = logging.getLogger("saarthi.companion")

WAKE_WORDS = re.compile(r"saa?r[ae]?thi|सारथी|सारथि|ਸਾਰਥੀ|સારથી|sarthi|sarathy", re.I)
CONVERSATION_OPEN_S = 90
ADDRESS_MODE = "wake"  # "wake": respond when addressed / in conversation / emergency; "always": respond to all speech

# Timbre v2.5 voice per language (change to taste after listening)
VOICES = {"hi-IN": "Poorvi", "pa-IN": "Zayan", "gu-IN": "Veera", "mr-IN": "Ishaan", "bn-IN": "Dhruva",
          "ta-IN": "Asmita", "te-IN": "Suhana", "kn-IN": "Kavin", "ml-IN": "Riyaan", "en-IN": "Pranav"}
GREETING = "राम राम बॉस! सारथी हाज़िर है। आज कहाँ का सफ़र है?"


def speakable(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[\U0001F000-\U0001FFFF☀-➿•]", "", text)
    return " ".join(text.split())


def addressed(text: str, last_bot_speech: float, mode: str = ADDRESS_MODE) -> bool:
    if mode == "always":
        return True
    if rule_severity(text):  # emergency or breakdown words always reach the agent
        return True
    if WAKE_WORDS.search(text):
        return True
    return time.time() - last_bot_speech < CONVERSATION_OPEN_S


class LiveSession(LiveTarget):
    def __init__(self, vehicle: dict, connection) -> None:
        self.vid = vehicle["id"]
        self.code = vehicle["code"]
        self.connection = connection
        self.worker = None
        self.turn = drv.Turn()
        self.last_bot_speech = time.time()
        self.lang = vehicle.get("lang") or "hi-IN"

    # ---------- delivery from other agents ----------
    async def deliver(self, out: Out) -> None:
        from pipecat.frames.frames import OutputTransportMessageUrgentFrame, TTSSpeakFrame

        frames = [OutputTransportMessageUrgentFrame(message={
            "type": "display", "text": out.text, "contacts": out.contacts,
            "location": out.location, "actions": out.actions})]
        say = speakable(out.speak or out.text)
        if say:
            frames.append(TTSSpeakFrame(say))
        if self.worker:
            await self.worker.queue_frames(frames)

    def refresh_context(self, v: dict) -> None:
        """Fresh situation (time, speed, landmark, unreported tolls...) in the system message every turn."""
        ctx = getattr(self, "context", None)
        if ctx is None:
            return
        try:
            msgs = ctx.get_messages()
            if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
                msgs[0]["content"] = drv.system_prompt(v, is_moving(v), live=True)
        except Exception:
            log.exception("context refresh failed")

    # ---------- pipeline ----------
    def _tools(self):
        from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
        from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema

        schemas = []
        for tool in drv.make_tools(self.vid, self.turn):
            async def handler(params, _tool=tool):
                try:
                    result = await _tool.fn(**dict(params.arguments))
                except Exception as e:
                    log.exception("tool %s failed", _tool.name)
                    result = {"error": str(e)}
                db.log_agent(f"driver:{self.code}", "tool", _tool.name, {"args": dict(params.arguments)})
                await params.result_callback(result)

            p = tool.parameters
            schemas.append(FunctionSchema(name=tool.name, description=tool.description,
                                          properties=p.get("properties", {}), required=p.get("required", []),
                                          handler=handler))
        return ToolsSchema(standard_tools=schemas)

    async def run(self) -> None:
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.runner import PipelineRunner
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (LLMContextAggregatorPair,
                                                                           LLMUserAggregatorParams)
        from pipecat.frames.frames import TTSSpeakFrame
        from pipecat.services.openai.llm import OpenAILLMService
        from pipecat.transports.base_transport import TransportParams
        from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
        from pipecat_gnani import GnaniHttpSTTService, GnaniSSETTSService

        v = db.vehicle(self.vid)
        async with aiohttp.ClientSession() as http:
            transport = SmallWebRTCTransport(webrtc_connection=self.connection,
                                             params=TransportParams(audio_in_enabled=True, audio_out_enabled=True))
            cands = list(dict.fromkeys([self.lang, *settings.stt_lang_candidates]))[:4]
            stt = GnaniHttpSTTService(
                api_key=settings.gnani_api_key, aiohttp_session=http,
                settings=GnaniHttpSTTService.Settings(
                    model="prisma-v3.0", language="indic-auto", language_candidates=cands if len(cands) >= 2 else None,
                    format="transcribe", keywords=load_keyterms() or None, topic=DEFAULT_TOPIC))
            # SSE = one HTTP stream per reply: survives network drops on highways (a dropped WebSocket would
            # silence the companion until reconnect)
            tts = GnaniSSETTSService(api_key=settings.gnani_api_key, aiohttp_session=http, settings=GnaniSSETTSService.Settings(
                model=settings.tts_model, voice=VOICES.get(self.lang, settings.tts_voice), language=self.lang))
            from .config import llm_providers
            cfg = llm_providers() or [{"api_key": settings.llm_api_key, "base_url": settings.llm_base_url,
                                       "model": settings.llm_model}]
            # Voice can't wait: a busy model (Gemini 503) loses that whole turn. Fast providers (Groq) answer
            # first when configured; Gemini models stay as backups. VOICE_LLM_FIRST=gemini keeps Gemini first.
            if os.getenv("VOICE_LLM_FIRST", "fast") != "gemini":
                cfg = sorted(cfg, key=lambda p: "generativelanguage.googleapis.com" in p["base_url"])
            llms = [_voice_llm(p) for p in cfg[:4]]
            if len(llms) == 1:
                llm = llms[0]
            else:
                from pipecat.pipeline.llm_switcher import LLMSwitcher
                from pipecat.pipeline.service_switcher import ServiceSwitcherStrategyFailover
                llm = LLMSwitcher(llms=llms, strategy_type=ServiceSwitcherStrategyFailover)
            context = LLMContext(messages=[{"role": "system", "content": drv.system_prompt(v, is_moving(v), live=True)}],
                                 tools=self._tools())
            self.context = context
            agg = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()))
            guard = CompanionGuard(self)
            spoken = SpokenLog(self)
            pipeline = Pipeline([transport.input(), stt, guard, agg.user(), llm, spoken, tts, transport.output(),
                                 agg.assistant()])
            self.worker = PipelineWorker(pipeline, params=PipelineParams(enable_metrics=True),
                                         cancel_on_idle_timeout=False)

            @transport.event_handler("on_client_connected")
            async def _connected(t, client):
                bus.live[self.vid] = self
                db.log_agent(f"driver:{self.code}", "system", "companion_online", {})
                db.log_agent("system", f"driver:{self.code}", "message",
                             {"text": GREETING, "vehicle": self.code, "voice": True})
                await self.worker.queue_frames([TTSSpeakFrame(GREETING)])

            @transport.event_handler("on_client_disconnected")
            async def _disconnected(t, client):
                if bus.live.get(self.vid) is self:
                    bus.live.pop(self.vid, None)
                db.log_agent(f"driver:{self.code}", "system", "companion_offline", {})
                await self.worker.cancel()

            try:
                runner = PipelineRunner(handle_sigint=False)
                await runner.add_workers(self.worker)
                await runner.run()
            finally:
                if bus.live.get(self.vid) is self:
                    bus.live.pop(self.vid, None)


def _voice_llm(p: dict):
    """Gemini through Pipecat's native Google service (multi-step tool calls need Gemini's thought signatures);
    any other OpenAI-compatible provider (Groq, vLLM...) through the OpenAI service."""
    if "generativelanguage.googleapis.com" in p["base_url"]:
        from pipecat.services.google.llm import GoogleLLMService
        return GoogleLLMService(api_key=p["api_key"], settings=GoogleLLMService.Settings(model=p["model"]))
    from pipecat.services.openai.llm import OpenAILLMService
    return OpenAILLMService(api_key=p["api_key"], base_url=p["base_url"], settings=OpenAILLMService.Settings(model=p["model"]))


def _guard_base():
    from pipecat.processors.frame_processor import FrameProcessor
    return FrameProcessor


class SpokenLog(_guard_base()):
    """Collects each reply the LLM speaks and logs it, so the office sees the whole voice conversation."""

    def __init__(self, session: "LiveSession") -> None:
        super().__init__()
        self.s = session
        self.buf: list[str] = []

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import LLMFullResponseEndFrame, LLMTextFrame
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self.buf.append(frame.text or "")
        elif isinstance(frame, LLMFullResponseEndFrame) and self.buf:
            text = "".join(self.buf).strip()
            self.buf = []
            if text:
                db.log_agent("system", f"driver:{self.s.code}", "message",
                             {"text": text, "vehicle": self.s.code, "voice": True})
        await self.push_frame(frame, direction)


class CompanionGuard(_guard_base()):
    """Deterministic layer between speech recognition and the LLM."""

    def __init__(self, session: LiveSession) -> None:
        super().__init__()
        self.s = session

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, InputTransportMessageFrame,
                                           TranscriptionFrame, TTSSpeakFrame, TTSUpdateSettingsFrame)
        await super().process_frame(frame, direction)

        if isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
            self.s.last_bot_speech = time.time()
        elif isinstance(frame, InputTransportMessageFrame):
            await self._client_message(frame.message)
        elif isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            raw = getattr(frame, "language", None)
            lang = str(getattr(raw, "value", raw) or "")
            if lang in VOICES and lang != self.s.lang:
                self.s.lang = lang
                db.ex("UPDATE vehicles SET lang=? WHERE id=?", (lang, self.s.vid))
                await self.push_frame(TTSUpdateSettingsFrame(settings={"voice": VOICES[lang], "language": lang}))
            if text:
                db.log_agent(f"driver:{self.s.code}", "driver_agent", "driver_said",
                             {"text": text, "lang": lang, "vehicle": self.s.code, "voice": True})
            v = db.vehicle(self.s.vid)
            reply = await drv.intercept(v, text) if text else None
            if reply:
                await self.push_frame(TTSSpeakFrame(reply))
                return  # handled in code; do not send to the LLM
            if not text or not addressed(text, self.s.last_bot_speech):
                return  # driver is talking to someone else; stay quiet
            self.s.turn.transcript = text
            self.s.turn.new_pending = False
            self.s.refresh_context(v)
        await self.push_frame(frame, direction)

    async def _client_message(self, msg) -> None:
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        if kind in ("incoming_call", "call_ended", "call_active", "notification"):
            await drv.native_event(db.vehicle(self.s.vid), msg)
            return
        if msg.get("type") == "location" and msg.get("lat") is not None:
            from .demo_drive import is_simulated
            if is_simulated(self.s.vid):
                return
            speed = msg.get("speed")  # m/s from the phone's GPS, may be null
            await fatigue.location_update(self.s.vid, float(msg["lat"]), float(msg["lon"]),
                                          speed * 3.6 if isinstance(speed, (int, float)) else None)


# ---------- WebRTC signalling (called from web.py) ----------
_handler = None


def handler():
    global _handler
    if _handler is None:
        from pipecat.transports.smallwebrtc.connection import IceServer
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler
        _handler = SmallWebRTCRequestHandler(ice_servers=[IceServer(**s) for s in settings.ice_servers])
    return _handler


async def offer(vehicle: dict, body: dict) -> dict | None:
    from pipecat.transports.smallwebrtc.connection import IceServer
    from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest
    from . import turn
    # fresh TURN credentials for the server side of each new call (cached, refreshed before expiry)
    handler().update_ice_servers([IceServer(**s) for s in await turn.ice_servers()])

    async def on_connection(connection):
        old = bus.live.get(vehicle["id"])
        if old and old.worker:
            await old.worker.cancel()
        asyncio.create_task(LiveSession(vehicle, connection).run())

    req = SmallWebRTCRequest(sdp=body["sdp"], type=body["type"], pc_id=body.get("pc_id"),
                             restart_pc=body.get("restart_pc"), request_data={"vehicle": vehicle["code"]})
    return await handler().handle_web_request(request=req, webrtc_connection_callback=on_connection)


async def ice(body: dict) -> None:
    from pipecat.transports.smallwebrtc.request_handler import IceCandidate, SmallWebRTCPatchRequest

    await handler().handle_patch_request(SmallWebRTCPatchRequest(
        pc_id=body["pc_id"], candidates=[IceCandidate(**c) for c in body.get("candidates", [])]))
