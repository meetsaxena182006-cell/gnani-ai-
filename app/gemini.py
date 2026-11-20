"""Gemini brain. Uses the Gemini API key from .env locally; on Cloud Run it switches to Vertex AI
(service-account auth, no key). Every call has a per-model timeout, a model fallback chain and a safe
template fallback so the demo never stalls."""
from __future__ import annotations

import asyncio
import os

from google import genai
from google.genai import types

_client = None
_mode = "offline"
# Preferred order; the first model that answers is promoted to the front and reused.
DEFAULT_CHAIN = ["gemini-3-flash-preview", "gemini-3.8-flash", "gemini-3.5-flash", "gemini-flash-latest"]
CHAIN: list[str] = []
MODELS = {"fast": None, "pro": None}


def init():
    global _client, _mode, CHAIN
    key = os.getenv("GEMINI_API_KEY", "").strip()
    use_vertex = os.getenv("USE_VERTEX", "").lower() in ("1", "true")
    try:
        if use_vertex:
            _client = genai.Client(vertexai=True, project=os.getenv("GOOGLE_CLOUD_PROJECT"),
                                   location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"))
            _mode = "vertex"
        elif key:
            _client = genai.Client(api_key=key)
            _mode = "gemini-api"
        else:
            return status()
        env_chain = [m.strip() for m in os.getenv("GEMINI_MODELS", "").split(",") if m.strip()]
        CHAIN = env_chain or list(DEFAULT_CHAIN)
        MODELS["fast"] = CHAIN[0]
        MODELS["pro"] = os.getenv("GEMINI_MODEL_PRO", "gemini-3.1-pro-preview")
    except Exception as e:  # noqa: BLE001
        _client, _mode = None, f"offline ({type(e).__name__})"
    return status()


def status():
    return {"mode": _mode, "fast": MODELS["fast"], "pro": MODELS["pro"], "chain": CHAIN, "ready": _client is not None,
            "last_error": LAST_ERROR["msg"] if "LAST_ERROR" in globals() else ""}


def ready():
    return _client is not None


def _promote(name):
    if name in CHAIN and CHAIN[0] != name:
        CHAIN.remove(name)
        CHAIN.insert(0, name)
    MODELS["fast"] = name


_cool: dict[str, float] = {}      # model -> time until which it is skipped (rate limited / failing)
_net_down_until = 0.0              # whole-API circuit breaker (network down)
LAST_ERROR = {"msg": ""}


async def _call(contents, cfg, total_timeout, per_model=8.0):
    global _net_down_until
    loop = asyncio.get_running_loop()
    now = loop.time()
    if now < _net_down_until:
        return None
    deadline = now + total_timeout
    for name in list(CHAIN):
        if _cool.get(name, 0) > loop.time():
            continue
        left = deadline - loop.time()
        if left <= 0.5:
            break
        try:
            r = await asyncio.wait_for(_client.aio.models.generate_content(model=name, contents=contents, config=cfg),
                                       min(per_model, left))
            _promote(name)
            return r
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            LAST_ERROR["msg"] = f"{name}: {msg[:160]}"
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                _cool[name] = loop.time() + 60
            elif "503" in msg or "UNAVAILABLE" in msg or "404" in msg:
                _cool[name] = loop.time() + 120
            elif "getaddrinfo" in msg or "10053" in msg or "Cannot connect" in msg or "ConnectError" in type(e).__name__:
                _net_down_until = loop.time() + 20   # network is down: stop trying for 20 s
                return None
            elif isinstance(e, asyncio.TimeoutError):
                _cool[name] = loop.time() + 30
            continue
    return None


async def generate(prompt: str, system: str = "", model: str = "fast", timeout: float = 12.0, json_mode=False):
    if not _client:
        return None
    cfg = types.GenerateContentConfig(system_instruction=system or None, temperature=0.4,
                                      response_mime_type="application/json" if json_mode else None)
    r = await _call(prompt, cfg, timeout)
    try:
        return r.text.strip() if r and r.text else None
    except Exception:
        return None


async def chat_with_tools(history: list[dict], system: str, tools: list[dict], executor, timeout: float = 14.0):
    """history: [{'role':'user'|'model','text':...}]. executor(name, args) -> dict (async)."""
    if not _client:
        return None, []
    decls = [types.FunctionDeclaration(name=t["name"], description=t["description"],
                                       parameters=t.get("parameters") or {"type": "object", "properties": {}})
             for t in tools]
    cfg = types.GenerateContentConfig(system_instruction=system, temperature=0.3,
                                      tools=[types.Tool(function_declarations=decls)])
    contents = [types.Content(role=h["role"], parts=[types.Part(text=h["text"])]) for h in history]
    calls_made = []
    try:
        for _ in range(4):
            r = await _call(contents, cfg, timeout, per_model=10.0)
            if r is None:
                return None, calls_made
            fcs = r.function_calls or []
            if not fcs:
                return (r.text or "").strip() or None, calls_made
            contents.append(r.candidates[0].content)
            parts = []
            for fc in fcs:
                args = dict(fc.args or {})
                result = await executor(fc.name, args)
                calls_made.append({"tool": fc.name, "args": args})
                parts.append(types.Part.from_function_response(name=fc.name, response={"result": result}))
            contents.append(types.Content(role="user", parts=parts))
        return "Done.", calls_made
    except Exception:
        return None, calls_made
