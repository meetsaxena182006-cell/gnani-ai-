"""FILM MODE: the whole SIMULATED Udaipur -> Hyderabad demo trip, captured once and played back smoothly for recording.

1. capture  - runs every scenario step at full speed (no waits) against the real system (watchdog, FASTag tolls,
               breakdown flow, Fleet Mesh, planner, invoices) and records every line Saarthi / the CEO Agent says, plus a
               few driver / owner questions answered live by the real agents (Groq / Gemini).
2. voice    - every spoken line is turned into audio with Gnani Timbre v2.5 (TTS), ahead of time.
3. playback - /film plays it like a video: phone + office side by side, moving truck on the map, voices in order.

Nothing waits on the network while you record, so the video has no lag. Everything is demo data and labelled so.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from . import db, scenario
from .config import settings

log = logging.getLogger("saarthi.film")

DIR = os.path.join(os.path.dirname(settings.db_path) or ".", "film")
F: dict = {"status": "idle", "progress": "", "error": None, "timeline": None, "task": None}
_audio: dict[int, bytes] = {}

# role -> Gnani Timbre voices to try (first that works wins)
VOICE_TRY = {"saarthi": ["Poorvi", "Suhana", "Veera"], "driver": ["Zayan", "Ishaan", "Kavin", "Riyaan", "Pranav"],
             "ceo": ["Pranav", "Dhruva"], "owner": ["Riyaan", "Kavin", "Dhruva", "Ishaan"]}
_voice_ok: dict[str, str] = {}

# real questions put to the real agents during capture: (after step index, who, text)
ASK = [(3, "driver", "हाँ सारथी, डीज़ल भरवा लिया — 235 लीटर, 21620 रुपये।"),
       (3, "driver", "हाँ, सही है।"),
       (7, "driver", "सारथी, अभी कितना बाकी है और कब तक पहुँचेंगे?"),
       (12, "owner", "हाँ, वापसी वाला हैदराबाद से उदयपुर का लोड बुक कर दो।"),
       (13, "owner", "इस ट्रिप में कुल कितना ख़र्चा हुआ और कितना बचा? ड्राइवर का हिसाब भी बताओ।")]

FAIL_TEXT = "samajh nahi paaya"


async def _owner_voice_answer(fleet: dict, text: str) -> str:
    """The CEO Agent exactly as on the owner's voice call: same prompt + voice rules + tools, spoken-style answer."""
    from .agents import ceo
    from .llm import run_agent
    from .owner_voice import VOICE_RULES
    key = f"owner:{fleet['id']}"
    reply = await run_agent(ceo.system_prompt(fleet) + VOICE_RULES, ceo.memory.get(key), text,
                            ceo._fleet_tools(fleet["id"]), agent_name=f"ceo:{fleet['code']}")
    if reply and FAIL_TEXT not in reply:
        ceo.memory.add(key, "user", text)
        ceo.memory.add(key, "assistant", reply)
    return reply


_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def _clean(text: str) -> str:
    t = _EMOJI.sub("", text or "")
    t = re.sub(r"[*_#`>|]", "", t)
    t = re.sub(r"\(demo\)", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _chunks(text: str, n: int = 260) -> list[str]:
    parts, cur = [], ""
    for s in re.split(r"(?<=[।.!?])\s+", text):
        if len(cur) + len(s) > n and cur:
            parts.append(cur.strip())
            cur = ""
        cur += s + " "
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _stamp(ev: dict) -> dict:
    S = scenario.S
    ev.setdefault("step", S["step"] + 1)
    ev["km"] = round(S["km"], 1)
    try:
        la, lo = scenario._point(S["km"])
        ev["pos"] = [round(la, 4), round(lo, 4)]
    except Exception:
        pass
    try:
        ev["money"] = scenario.money()
    except Exception:
        pass
    return ev


async def _capture() -> list[dict]:
    from .agents import ceo
    from .agents import driver as drv
    S = scenario.S
    events: list[dict] = []
    orig = (scenario.driver_say, scenario.ceo_tell, scenario.note)

    async def driver_say(text, card=None, **kw):
        events.append(_stamp({"kind": "saarthi", "text": card or text, "speak": text}))
        await orig[0](text, card, **kw)

    async def ceo_tell(text, speak=None):
        events.append(_stamp({"kind": "ceo", "text": text, "speak": speak}))
        await orig[1](text, None)          # office inbox only (no live voice during capture)

    def note(text):
        events.append(_stamp({"kind": "note", "text": text}))
        orig[2](text)

    scenario.driver_say, scenario.ceo_tell, scenario.note = driver_say, ceo_tell, note
    scenario.stop_auto()
    S.update(step=-1, log=[], km=0.0, fast=True, film=events, final=None)
    try:
        for i, (title, fn) in enumerate(scenario.STEPS):
            F["progress"] = f"capturing {i + 1}/{len(scenario.STEPS)}: {title}"
            events.append({"kind": "step", "step": i, "text": title})
            try:
                await fn()
            except Exception as e:
                log.exception("film step failed")
                events.append(_stamp({"kind": "note", "text": f"⚠️ step failed: {e}"}))
            S["step"] = i
            for after, who, q in ASK:
                if after != i:
                    continue
                v = scenario.hero()
                events.append(_stamp({"kind": who, "text": q, "speak": q, "step": i}))
                try:
                    reply = None
                    for attempt in range(3):
                        if who == "driver":
                            reply = await asyncio.wait_for(drv.handle(v, q), 45)
                        else:
                            reply = await asyncio.wait_for(_owner_voice_answer(db.fleet(v["fleet_id"]), q), 60)
                        if reply and FAIL_TEXT not in reply:
                            break
                        await asyncio.sleep(6)
                    if not reply or FAIL_TEXT in reply:
                        raise RuntimeError("agent did not answer")
                    kind = "saarthi" if who == "driver" else "ceo"
                    events.append(_stamp({"kind": kind, "text": reply, "speak": reply, "live": True, "step": i}))
                except Exception as e:
                    log.warning("film: agent question failed: %s", e)
                    events.pop()       # no answer -> leave the question out (never invent the agent's reply)
    finally:
        scenario.driver_say, scenario.ceo_tell, scenario.note = orig
        S.update(fast=False, film=None)
    return events


def _tts(text: str, role: str) -> bytes:
    from gnani.tts import AudioConfig, GnaniTTSClient
    c = GnaniTTSClient(api_key=settings.gnani_api_key)
    cfg = AudioConfig(sample_rate=48000, encoding="oggopus", container="raw")
    tries = [_voice_ok[role]] if role in _voice_ok else VOICE_TRY[role]
    err = None
    for voice in tries:
        try:
            b = c.synthesize(text, voice=voice, model=settings.tts_model, language="hi-IN", audio_config=cfg)
            _voice_ok[role] = voice
            return b
        except Exception as e:
            err = e
    raise RuntimeError(f"TTS failed for {role}: {err}")


async def _voice(events: list[dict]) -> None:
    os.makedirs(DIR, exist_ok=True)
    _audio.clear()
    n = 0
    spoken = [e for e in events if e.get("speak")]
    for k, ev in enumerate(spoken):
        F["progress"] = f"Gnani Timbre voices {k + 1}/{len(spoken)}"
        ev["audio"] = []
        for part in _chunks(_clean(ev["speak"])):
            try:
                b = await asyncio.to_thread(_tts, part, ev["kind"] if ev["kind"] in VOICE_TRY else "saarthi")
            except Exception as e:
                log.warning("film TTS: %s", e)
                continue
            _audio[n] = b
            with open(os.path.join(DIR, f"{n:03d}.ogg"), "wb") as fh:
                fh.write(b)
            ev["audio"].append(n)
            n += 1
        ev["voice"] = _voice_ok.get(ev["kind"] if ev["kind"] in VOICE_TRY else "saarthi")


async def _prepare() -> None:
    try:
        F.update(status="capturing", error=None)
        events = await _capture()
        F["status"] = "voicing"
        await _voice(events)
        route = [[round(a, 4), round(b, 4), round(c, 1)] for (a, b), c in zip(scenario._route, scenario._cum)]
        tl = {"events": events, "route": route, "total_km": round(scenario.total_km()), "steps": [s[0] for s in scenario.STEPS],
              "final": scenario.S.get("final"), "voices": dict(_voice_ok), "tts_model": settings.tts_model,
              "client": scenario.CLIENT, "freight": scenario.FREIGHT}
        os.makedirs(DIR, exist_ok=True)
        with open(os.path.join(DIR, "timeline.json"), "w", encoding="utf-8") as fh:
            json.dump(tl, fh, ensure_ascii=False)
        F.update(status="ready", timeline=tl, progress=f"ready: {len(events)} events, {len(_audio)} voice clips")
    except Exception as e:
        log.exception("film prepare failed")
        F.update(status="error", error=str(e))


def start() -> dict:
    if F["status"] not in ("capturing", "voicing"):
        F["task"] = asyncio.create_task(_prepare())
        F["status"] = "capturing"
    return state()


def timeline() -> dict | None:
    if F["timeline"] is None:
        p = os.path.join(DIR, "timeline.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                F["timeline"] = json.load(fh)
            F["status"] = "ready"
    return F["timeline"]


def audio(n: int) -> bytes | None:
    if n not in _audio:
        p = os.path.join(DIR, f"{n:03d}.ogg")
        if os.path.exists(p):
            with open(p, "rb") as fh:
                _audio[n] = fh.read()
    return _audio.get(n)


def state() -> dict:
    timeline()
    return {"status": F["status"], "progress": F["progress"], "error": F["error"],
            "events": len((F["timeline"] or {}).get("events", []))}
