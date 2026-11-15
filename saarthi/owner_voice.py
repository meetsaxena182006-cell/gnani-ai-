"""Voice-first CEO Agent for the owner: talk to the office manager like on a phone call.

Office page mic  <-- WebRTC -->  [Gnani Prisma v3.0 streaming STT] -> context -> LLM with every CEO tool
(fleet health, live locations, tolls, diesel, documents/insurance, freight quotes, trip profit, message a driver)
-> [Gnani Timbre v2.5 TTS] -> speaker.  Everything said both ways lands in the office Conversations panel.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import aiohttp

from . import db
from .agents import ceo
from .companion import clean_args
from .config import settings

log = logging.getLogger("saarthi.owner_voice")

GREETING = "नमस्ते मालिक! मैं आपका CEO सारथी हूँ। सब गाड़ियों पर मेरी नज़र है — बताइए, क्या जानना है?"
VOICE_RULES = """
VOICE CALL MODE (you are speaking on a live call with the owner):
- Speak like a sharp, warm office manager on the phone: natural Hindi/Hinglish, short spoken sentences, no lists,
  no markdown, no emojis, no links. Numbers spoken simply ("ek lakh pachees hazaar rupaye" or "₹1,25,000").
- Write Hindi in Devanagari script (the voice reads it); English words like diesel, toll, FASTag, PUC stay as they are.
- First the headline in one sentence, then offer detail ("aur detail chahiye?"). Keep each reply under ~45 words
  unless the owner asks for the full breakdown (then give the calculation step by step).
- Be proactive: after answering, mention the one most important thing that needs the owner's attention
  (an unreported toll, an expiring paper/insurance, a loss-making trip, a speeding driver) if there is one.
- ALWAYS use tools for facts. Never guess money, locations, papers or statuses.
- YOU RUN THE WHOLE WORKFLOW BY VOICE. The owner only speaks:
  * New trip ("Udaipur se Haridwar, Ramesh ki gaadi", "MW-T2 ko Surat bhejo"): if the truck/driver or destination is
    missing ask ONE short question, then call assign_trip (vehicle_code can be the driver's first name, the code or the
    number; freight/client optional). Then say: trip set, driver told, full plan (route, breaks, diesel, tolls, weather,
    ETA, profit) coming in about 30 seconds. If the owner wants to watch it run, call assign_trip with simulate_drive=true.
  * New order / deal ("naya order: Pune se Hyderabad 18 ton 52000 Deccan Agro"): add_order, then ask which truck.
  * Message for a driver ("Ramesh ko bolo dhaba pe ruk ke khana kha le"): message_driver — his agent SPEAKS it in the cab.
  * New truck: add_truck. Profit, papers, tolls, payments, loads for empty trucks: use the matching tool."""


def opener(fleet: dict) -> str:
    """Jarvis-style: the CEO speaks first, gives the one-line state and asks for today's plan."""
    try:
        from . import sentinel
        from .talkfirst import _first
        h = sentinel.fleet_health(fleet["id"])
        name = _first(fleet.get("owner_name"))
        who = "मालिक" if name in ("बॉस", "", None) else f"{name} जी"
        n = len(h["trucks"])
        if not n:
            return (f"नमस्ते {who}! मैं आपका CEO सारथी। अभी आपके ऑफिस में कोई गाड़ी नहीं है। "
                    "बोलिए — गाड़ी का नंबर, ड्राइवर का नाम और गाड़ी कहाँ खड़ी है, मैं जोड़ देता हूँ।")
        bad = [t for t in h["trucks"] if t["status"] == "bad"]
        idle = [t for t in h["trucks"] if not t["trip"]]
        state = (f"{len(bad)} गाड़ी पर ध्यान चाहिए" if bad else "सब गाड़ियाँ ठीक चल रही हैं")
        free = f", {len(idle)} गाड़ी खाली है" if idle else ""
        return (f"नमस्ते {who}! मैं आपका CEO सारथी। {n} गाड़ियों में {state}{free}। "
                "आज का क्या प्लान है? कोई नई ट्रिप लगानी है? बस बोलिए — कौनसी गाड़ी, कहाँ से कहाँ।")
    except Exception:
        return GREETING


class OwnerSession:
    def __init__(self, fleet: dict, connection) -> None:
        self.fleet = fleet
        self.connection = connection
        self.worker = None
        self.last_speech = time.time()

    async def _rerun(self) -> None:
        from pipecat.frames.frames import LLMRunFrame
        if self.worker:
            await self.worker.queue_frames([LLMRunFrame()])

    def _tools(self):
        from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
        from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema

        schemas = []
        for tool in ceo._fleet_tools(self.fleet["id"]):
            async def handler(params, _tool=tool):
                try:
                    result = await _tool.fn(**clean_args(_tool, params.arguments))
                except Exception as e:
                    log.exception("tool %s failed", _tool.name)
                    result = {"error": str(e)}
                db.log_agent(f"ceo:{self.fleet['code']}", "tool", _tool.name, {"args": dict(params.arguments)})
                await params.result_callback(result)

            p = tool.parameters
            schemas.append(FunctionSchema(name=tool.name, description=tool.description,
                                          properties=p.get("properties", {}), required=p.get("required", []),
                                          handler=handler))
        return ToolsSchema(standard_tools=schemas)

    async def run(self) -> None:
        from pipecat.frames.frames import TTSSpeakFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.runner import PipelineRunner
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
        from pipecat.transports.base_transport import TransportParams
        from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
        from pipecat_gnani import GnaniSSETTSService, GnaniSTTService

        from .companion import VOICES, _llm_for_voice, _local_vad, _user_params

        code = self.fleet["code"]
        async with aiohttp.ClientSession() as http:
            transport = SmallWebRTCTransport(webrtc_connection=self.connection,
                                             params=TransportParams(audio_in_enabled=True, audio_out_enabled=True))
            cands = list(dict.fromkeys(["hi-IN", *settings.stt_lang_candidates]))[:4]
            stt = GnaniSTTService(api_key=settings.gnani_api_key, keepalive_timeout=8.0,
                                  settings=GnaniSTTService.Settings(
                                      model="prisma-v3.0", language="indic-auto",
                                      language_candidates=cands if len(cands) >= 2 else None, format="transcribe"))
            tts = GnaniSSETTSService(api_key=settings.gnani_api_key, aiohttp_session=http,
                                     settings=GnaniSSETTSService.Settings(
                                         model=settings.tts_model, voice=VOICES.get("en-IN", settings.tts_voice),
                                         language="hi-IN"))
            context = LLMContext(messages=[{"role": "system", "content": ceo.system_prompt(self.fleet) + VOICE_RULES}],
                                 tools=self._tools())
            agg = LLMContextAggregatorPair(context, user_params=_user_params(_local_vad()))
            pipeline = Pipeline([transport.input(), stt, OwnerHeard(code), agg.user(), _llm_for_voice(rerun=self._rerun),
                                 OwnerSpoken(code), tts, transport.output(), agg.assistant()])
            self.worker = PipelineWorker(pipeline, params=PipelineParams(enable_metrics=True),
                                         cancel_on_idle_timeout=False)

            @transport.event_handler("on_client_connected")
            async def _connected(t, client):
                hello = opener(self.fleet)
                db.log_agent(f"ceo:{code}", f"owner:{code}", "message", {"text": hello, "voice": True})
                await self.worker.queue_frames([TTSSpeakFrame(hello)])

            @transport.event_handler("on_client_disconnected")
            async def _disconnected(t, client):
                await self.worker.cancel()

            runner = PipelineRunner(handle_sigint=False)
            await runner.add_workers(self.worker)
            await runner.run()


def _base():
    from pipecat.processors.frame_processor import FrameProcessor
    return FrameProcessor


class OwnerHeard(_base()):
    """Logs what the owner said (Gnani transcript) into the office Conversations panel."""

    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import TranscriptionFrame
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            # background chatter / half words: a real request has at least 2 words
            if len(text) < 4 or len(text.split()) < 2:
                return
            db.log_agent(f"owner:{self.code}", f"ceo:{self.code}", "owner_said", {"text": text, "voice": True})
            try:   # orders, trips-to-driver messages, routes, papers, month: answered by code in < 1 s
                from .fastlane import owner_fast
                from .workspace import fleet_by_code
                fast = await asyncio.wait_for(owner_fast(fleet_by_code(self.code), text), 8)
            except Exception as e:
                log.warning("owner voice fast lane skipped: %s", e)
                fast = None
            if fast:
                from pipecat.frames.frames import TTSSpeakFrame
                db.log_agent(f"ceo:{self.code}", f"owner:{self.code}", "message", {"text": fast, "voice": True})
                spoken = re.sub(r"[\U0001F000-\U0001FAFF☀-➿✅️]", "", fast).replace("₹", "")
                await self.push_frame(TTSSpeakFrame(spoken[:500]))
                return
        await self.push_frame(frame, direction)


class OwnerSpoken(_base()):
    """Logs each spoken CEO reply."""

    def __init__(self, code: str) -> None:
        super().__init__()
        self.code, self.buf = code, []

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import LLMFullResponseEndFrame, LLMTextFrame
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self.buf.append(frame.text or "")
        elif isinstance(frame, LLMFullResponseEndFrame) and self.buf:
            text, self.buf = "".join(self.buf).strip(), []
            if text:
                db.log_agent(f"ceo:{self.code}", f"owner:{self.code}", "message", {"text": text, "voice": True})
        await self.push_frame(frame, direction)


_sessions: dict[int, OwnerSession] = {}


async def offer(fleet: dict, body: dict) -> dict | None:
    from pipecat.transports.smallwebrtc.connection import IceServer
    from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest

    from . import turn
    from .companion import handler
    handler().update_ice_servers([IceServer(**s) for s in await turn.ice_servers()])

    async def on_connection(connection):
        old = _sessions.get(fleet["id"])
        if old and old.worker:
            await old.worker.cancel()
        s = _sessions[fleet["id"]] = OwnerSession(fleet, connection)
        asyncio.create_task(s.run())

    req = SmallWebRTCRequest(sdp=body["sdp"], type=body["type"], pc_id=body.get("pc_id"),
                             restart_pc=body.get("restart_pc"), request_data={"fleet": fleet["code"]})
    return await handler().handle_web_request(request=req, webrtc_connection_callback=on_connection)
