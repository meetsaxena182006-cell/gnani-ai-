"""Gnani Prisma (STT) and Timbre (TTS) wrapper.

- Uses the official `gnani-vachana` SDK.
- One request at a time (the API may allow only one concurrent request).
- SPEECH_MODE=mock is for offline development ONLY: STT reads a `.txt` sidecar next to the
  audio file, TTS returns no audio. Never use mock mode for evaluation or the demo.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .config import settings

log = logging.getLogger("saarthi.speech")
_sem = asyncio.Semaphore(1)
MAX_CLIP_SECONDS = 30
KEYTERMS_FILE = Path(__file__).resolve().parent.parent / "data" / "keyterms.txt"


def load_keyterms(path: Path = KEYTERMS_FILE) -> list[str]:
    if not path.exists():
        return []
    terms = [l.strip() for l in path.read_text(encoding="utf-8").splitlines()]
    terms = [t for t in terms if t and not t.startswith("#")]
    return terms[:200]


DEFAULT_TOPIC = (
    "A truck driver in India reporting trip expenses (diesel, toll, food, repairs), freight "
    "payments, breakdowns, locations and road conditions in Hindi, Hinglish or Rajasthani."
)


class Speech:
    def __init__(self) -> None:
        self.mode = settings.speech_mode
        self.keyterms = load_keyterms()
        self._stt = self._tts = None
        if self.mode == "gnani":
            from gnani.stt import GnaniSTTClient
            from gnani.tts import GnaniTTSClient

            self._stt = GnaniSTTClient(api_key=settings.gnani_api_key)
            self._tts = GnaniTTSClient(api_key=settings.gnani_api_key)
        else:
            log.warning("SPEECH_MODE=%s: Gnani APIs are NOT being called (dev only)", self.mode)

    # ---------- STT ----------
    def transcribe_sync(self, path: str | Path, use_keyterms: bool = True, language_code: str = "indic-auto") -> dict:
        path = Path(path)
        if self.mode != "gnani":
            side = path.with_suffix(".txt")
            text = side.read_text(encoding="utf-8").strip() if side.exists() else ""
            return {"transcript": text, "mock": True, "latency_s": 0.0}
        kwargs: dict = dict(
            model=settings.stt_model,
            format="transcribe",  # ITN: "chaar hazaar paanch sau" -> "₹4,500"
        )
        if settings.stt_model == "prisma-v3.0":
            kwargs["language_code"] = language_code
            if language_code == "indic-auto" and 2 <= len(settings.stt_lang_candidates) <= 4:
                kwargs["language_candidates"] = settings.stt_lang_candidates
            if use_keyterms and self.keyterms:
                kwargs["keywords"] = self.keyterms
                kwargs["topic"] = DEFAULT_TOPIC
        else:
            kwargs["language_code"] = "hi-IN" if language_code == "indic-auto" else language_code
        t0 = time.perf_counter()
        res = self._stt.transcribe(str(path), **kwargs)
        res["latency_s"] = round(time.perf_counter() - t0, 3)
        return res

    async def transcribe(self, path: str | Path, **kw) -> dict:
        async with _sem:
            return await asyncio.to_thread(self.transcribe_sync, path, **kw)

    # ---------- TTS ----------
    def synthesize_sync(self, text: str, language: str | None = None) -> bytes | None:
        """Return OGG/Opus bytes (Telegram voice-note format), or None in mock mode."""
        if self.mode != "gnani" or not text.strip():
            return None
        from gnani.tts import AudioConfig

        cfg = AudioConfig(sample_rate=48000, encoding="oggopus", container="raw")
        return self._tts.synthesize(
            text,
            voice=settings.tts_voice,
            model=settings.tts_model,
            language=language or settings.tts_language,
            audio_config=cfg,
        )

    async def synthesize(self, text: str, language: str | None = None) -> bytes | None:
        async with _sem:
            try:
                return await asyncio.to_thread(self.synthesize_sync, text, language)
            except Exception:  # voice is a bonus; the text reply always goes out
                log.exception("Timbre TTS failed; sending text only")
                return None


_speech: Speech | None = None


def get_speech() -> Speech:
    global _speech
    if _speech is None:
        _speech = Speech()
    return _speech
