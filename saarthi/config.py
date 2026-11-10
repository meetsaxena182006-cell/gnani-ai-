import json
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _secret(name: str) -> str:
    """A key from the environment; placeholders copied from .env.example count as not set."""
    v = (os.getenv(name) or "").strip()
    return "" if (not v or v.startswith("your_") or v == "telegram_token_from_botfather") else v


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    gnani_api_key: str = _secret("GNANI_API_KEY")
    llm_api_key: str = _secret("LLM_API_KEY")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    llm_model: str = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    driver_bot_token: str = _secret("DRIVER_BOT_TOKEN")
    owner_bot_token: str = _secret("OWNER_BOT_TOKEN")
    google_maps_key: str = _secret("GOOGLE_MAPS_API_KEY")
    public_base_url: str = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8080").rstrip("/")
    http_port: int = int(os.getenv("HTTP_PORT") or os.getenv("PORT") or "8080")  # cloud hosts set PORT
    n8n_webhook_url: str = os.getenv("N8N_WEBHOOK_URL", "")
    api_shared_secret: str = os.getenv("API_SHARED_SECRET", "change_me")
    stt_model: str = os.getenv("STT_MODEL", "prisma-v3.0")
    stt_lang_candidates: list[str] = field(default_factory=lambda: _list("STT_LANG_CANDIDATES", "hi-IN,en-IN"))
    tts_model: str = os.getenv("TTS_MODEL", "timbre-v2.5")
    tts_voice: str = os.getenv("TTS_VOICE", "Poorvi")
    tts_language: str = os.getenv("TTS_LANGUAGE", "hi-IN")
    speech_mode: str = os.getenv("SPEECH_MODE", "gnani")
    db_path: str = os.getenv("DB_PATH", "data/saarthi.db")
    # WebRTC: STUN by default. Mobile networks (CGNAT) usually also need a TURN server, e.g.
    # ICE_SERVERS=[{"urls":"stun:stun.l.google.com:19302"},{"urls":"turn:your.turn:3478","username":"u","credential":"p"}]
    ice_servers: list[dict] = field(default_factory=lambda: json.loads(
        os.getenv("ICE_SERVERS", '[{"urls": "stun:stun.l.google.com:19302"}]')))
    # Safety / behaviour
    fatigue_interval_min: int = int(os.getenv("FATIGUE_INTERVAL_MIN", "90"))  # companion check-in every 1-2 h
    mesh_radius_km: float = float(os.getenv("MESH_RADIUS_KM", "25"))
    moving_speed_kmph: float = float(os.getenv("MOVING_SPEED_KMPH", "8"))


def llm_providers() -> list[dict]:
    """Reasoning LLMs in priority order (OpenAI-compatible). Primary = LLM_*, fallback = LLM2_*.
    E.g. primary: Gnani-provided LLM endpoint or self-hosted Evon (vLLM); fallback: Gemini/Groq."""
    out = []
    for prefix in ("LLM", "LLM2", "LLM3"):
        key, url, model = _secret(f"{prefix}_API_KEY"), os.getenv(f"{prefix}_BASE_URL"), os.getenv(f"{prefix}_MODEL")
        if prefix == "LLM" and key:   # primary: Gemini defaults when only the key is given
            url, model = url or settings.llm_base_url, model or settings.llm_model
        if key and url and model:
            out.append({"name": os.getenv(f"{prefix}_NAME", prefix.lower()), "api_key": key, "base_url": url, "model": model})
    # Gemini models get busy (503) or retired (404): try sibling models on the same key before giving up
    fallbacks = _list("LLM_FALLBACK_MODELS", "gemini-flash-latest,gemini-2.5-flash,gemini-flash-lite-latest,gemini-2.5-flash-lite")
    extra = []
    for p in out:
        if "generativelanguage.googleapis.com" in p["base_url"]:
            for m in fallbacks:
                if all(m != q["model"] for q in out + extra if q["api_key"] == p["api_key"]):
                    extra.append({**p, "name": f"{p['name']}:{m}", "model": m})
    # Groq rate limits are per model: a sibling model on the same key keeps answering when one hits 429
    groq_more = _list("GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b")
    fast = []
    for p in out:
        if "api.groq.com" in p["base_url"]:
            for m in groq_more:
                if all(m != q["model"] for q in out + fast if q["api_key"] == p["api_key"]):
                    fast.append({**p, "name": f"{p['name']}:{m}", "model": m})
    return out + fast + extra


settings = Settings()
