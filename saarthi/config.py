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


KNOWN = {  # key prefix -> provider: base URL, fast talking models (in order), reasoning model
    "gsk_": ("groq", "https://api.groq.com/openai/v1", ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"], None),
    "nvapi-": ("nvidia", "https://integrate.api.nvidia.com/v1", ["nvidia/nemotron-3.5-lightning-30b-a3b"],
               "nvidia/nemotron-3-ultra-550b-a55b"),
    "sk-or-": ("openrouter", "https://openrouter.ai/api/v1", [], "nvidia/nemotron-3-ultra-550b-a55b:free"),
}


def _extra_keys() -> list[tuple]:
    """EXTRA_LLM_KEYS = comma-separated keys from Groq / NVIDIA / OpenRouter; the provider is detected from the prefix.
    One Render variable instead of a dozen. Returns (name, key, base_url, fast_models, reasoning_model)."""
    import re as _re
    raw = ""
    for name in ("EXTRA_LLM_KEYS", "EXTRA_LLM_KEY", "EXTRA_KEYS", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY2"):
        raw += "," + os.getenv(name, "")
    out, n = [], {}
    # tolerate paste mistakes: spaces, quotes, new lines, semicolons, "key=" labels
    toks = [t.strip().strip("'\"") for t in _re.split(r"[\s,;|]+", raw) if t.strip()]
    toks = [t.split("=", 1)[1] if "=" in t and not t.startswith(("gsk_", "nvapi-", "sk-or-")) else t for t in toks]
    for k in dict.fromkeys(toks):
        for pre, (name, url, fast, reason) in KNOWN.items():
            if k.startswith(pre):
                n[name] = n.get(name, 0) + 1
                out.append((f"{name}-x{n[name]}", k, url, fast, reason))
    return out


def llm_providers() -> list[dict]:
    """Talking LLMs (OpenAI-compatible). Each has a 'tier': lower = tried first, and the app rotates between
    providers of the same tier so one free-plan rate limit is never the bottleneck.
    0: big fast model on every Groq key, 1: second Groq model, 2: small Groq model, 3: NVIDIA, 5: other, 8-9: Gemini."""
    out = []
    for prefix in ("LLM", "LLM2", "LLM3", "LLM4", "LLM5", "LLM6"):
        key, url, model = _secret(f"{prefix}_API_KEY"), os.getenv(f"{prefix}_BASE_URL"), os.getenv(f"{prefix}_MODEL")
        key = (key or "").strip().strip("'\"")
        known = next((k for pre, k in KNOWN.items() if key.startswith(pre)), None)
        if known and (not url or known[1] not in url):   # a Groq/NVIDIA/OpenRouter key in any box: use its own server
            url, model = known[1], (known[2] or [known[3]])[0]
            out.append({"name": os.getenv(f"{prefix}_NAME", known[0] if prefix == "LLM" else f"{known[0]}-{prefix.lower()}"),
                        "api_key": key, "base_url": url, "model": model})
            continue
        if prefix == "LLM" and key:   # primary: Gemini defaults when only the key is given
            url, model = url or settings.llm_base_url, model or settings.llm_model
        if key and url and model:
            out.append({"name": os.getenv(f"{prefix}_NAME", prefix.lower()), "api_key": key, "base_url": url, "model": model})
    for name, key, url, fast, _ in _extra_keys():
        if fast and all(key != q["api_key"] for q in out):
            out.append({"name": name, "api_key": key, "base_url": url, "model": fast[0]})
    res = []
    groq_more = _list("GROQ_FALLBACK_MODELS", "openai/gpt-oss-120b,qwen/qwen3.8-27b,openai/gpt-oss-20b")
    gem_more = _list("LLM_FALLBACK_MODELS", "gemini-flash-latest,gemini-flash-lite-latest,gemini-3-flash-preview")
    for p in out:
        if "api.groq.com" in p["base_url"]:
            models = [p["model"]] + [m for m in groq_more if m != p["model"]]
            for t, m in enumerate(models):
                res.append({**p, "name": p["name"] if m == p["model"] else f"{p['name']}:{m}", "model": m, "tier": min(t, 2)})
        elif "nvidia.com" in p["base_url"]:
            res.append({**p, "tier": 3})
        elif "generativelanguage.googleapis.com" in p["base_url"]:
            res.append({**p, "tier": 8})
            for m in gem_more:
                if m != p["model"]:
                    res.append({**p, "name": f"{p['name']}:{m}", "model": m, "tier": 9})
        else:
            res.append({**p, "tier": 5})
    seen, uniq = set(), []
    for p in sorted(res, key=lambda p: p["tier"]):
        if (p["api_key"], p["model"]) not in seen:
            seen.add((p["api_key"], p["model"]))
            uniq.append(p)
    return uniq


def reasoning_providers() -> list[dict]:
    """Strong models for think_deeply / analyze_business: REASONING_* env first, then NVIDIA Nemotron Ultra,
    then OpenRouter's free copy of it (50 requests/day on a free account)."""
    out = []
    for prefix in ("REASONING", "REASONING2"):
        key, url, model = _secret(f"{prefix}_API_KEY"), os.getenv(f"{prefix}_BASE_URL"), os.getenv(f"{prefix}_MODEL")
        if key and url and model:
            out.append({"name": prefix.lower(), "api_key": key, "base_url": url, "model": model})
    for name, key, url, _, reason in sorted(_extra_keys(), key=lambda x: x[0].startswith("openrouter")):
        if reason:
            out.append({"name": f"{name}-reasoning", "api_key": key, "base_url": url, "model": reason})
    return out

settings = Settings()
