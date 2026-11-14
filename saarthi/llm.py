"""Minimal tool-calling agent loop over any OpenAI-compatible endpoint (Gemini, Groq, local Evon
via vLLM, ...). Speech is always Gnani; this is only the reasoning step."""
from __future__ import annotations

import os
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .config import settings

log = logging.getLogger("saarthi.llm")

ToolFn = Callable[..., Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: ToolFn

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def params(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


_client = None
_providers: list[tuple[object, str, str]] | None = None  # (client, model, name)


def providers() -> list[tuple[object, str, str]]:
    global _providers
    if _client is not None:  # injected (tests)
        return [(_client, settings.llm_model, "injected")]
    if _providers is None:
        from openai import AsyncOpenAI

        from .config import llm_providers
        cfg = llm_providers() or [{"name": "default", "api_key": settings.llm_api_key or "missing",
                                   "base_url": settings.llm_base_url, "model": settings.llm_model}]
        if os.getenv("LLM_FIRST", "fast") == "gemini":
            cfg = sorted(cfg, key=lambda p: "generativelanguage.googleapis.com" not in p["base_url"])
        clients: dict = {}
        _providers = []
        for p in cfg:
            c = clients.setdefault((p["api_key"], p["base_url"]), AsyncOpenAI(
                api_key=p["api_key"], base_url=p["base_url"], timeout=40 if "nvidia" in p["base_url"] else 25, max_retries=0))
            _providers.append((c, p["model"], p["name"]))
            _tier[p["name"]] = p.get("tier", 5)
    return _providers


def get_client():
    return providers()[0][0]


def set_client(client) -> None:
    """Tests inject a scripted fake client here."""
    global _client
    _client = client


_cool: dict[str, float] = {}   # provider name -> time it may be used again (rate limit / retired model)
_tier: dict[str, int] = {}
_rr = [0]                      # round-robin counter: spread load over providers of the same tier


def _cooldown(name: str, e: Exception) -> None:
    import re as _re
    import time as _t
    msg, code = str(e), getattr(e, "status_code", None)
    if code == 429 or "429" in msg[:20]:
        m = _re.search(r"try again in ([\d.]+)\s*(ms|s)", msg)
        wait = (float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)) if m else 20.0
        _cool[name] = _t.time() + min(wait + 0.5, 60)
    elif code in (400, 401, 403, 404):      # retired model / bad key: don't waste time on it for a while
        _cool[name] = _t.time() + 1800


async def _complete(messages: list[dict], tools: list["Tool"], agent_name: str):
    import asyncio as _aio
    import time as _t
    last_error = None
    for attempt in range(2):
        now = _t.time()
        _rr[0] += 1
        ready = [p for p in providers() if _cool.get(p[2], 0) <= now]
        tiers: dict = {}
        for p in ready:
            tiers.setdefault(_tier.get(p[2], 5), []).append(p)
        ready = [q for t in sorted(tiers) for q in (tiers[t][_rr[0] % len(tiers[t]):] + tiers[t][:_rr[0] % len(tiers[t])])]
        if not ready:      # everyone is rate-limited: wait for the first one to free up (max 12 s), then try it
            soon = min(providers(), key=lambda p: _cool.get(p[2], 0))
            await _aio.sleep(max(0.0, min(12.0, _cool.get(soon[2], 0) - now)))
            ready = [soon]
        for client, model, name in ready:
            try:
                # gpt-oss thinks before it talks; "low" answers in ~0.5 s instead of ~2 s with the same tool choice
                kw = {"extra_body": {"reasoning_effort": os.getenv("LLM_REASONING_EFFORT", "low")}} if "gpt-oss" in model else {}
                return await client.chat.completions.create(
                    model=model, messages=messages, tools=[t.schema() for t in tools] or None, temperature=0.2,
                    max_tokens=700, **kw)
            except Exception as e:  # provider down / quota: try the next one at once
                last_error = e
                _cooldown(name, e)
                log.warning("[%s] LLM provider %s failed: %s; trying next", agent_name, name, str(e)[:160])
    raise last_error


async def deep_reason(question: str, data: dict, role: str) -> str:
    """Slow, careful analysis by the strongest model (REASONING_* env, else the primary LLM).
    The talking agent stays fast; this is called as a tool for questions that need thinking."""
    import os
    from openai import AsyncOpenAI
    if _client is not None:
        client, model = _client, settings.llm_model
    else:
        import time as _tm
        from .config import reasoning_providers
        client = model = None
        chain = [(AsyncOpenAI(api_key=p["api_key"], base_url=p["base_url"], timeout=45, max_retries=0), p["model"], p["name"])
                 for p in reasoning_providers() if _cool.get(p["name"], 0) <= _tm.time()]
    prompt = (f"You are the analyst for {role} of a small Indian trucking business. Think step by step, privately, "
              "then answer in at most 5 short sentences of simple Hinglish with the key numbers. Use ONLY the data "
              "given; if something needed is missing, say what is missing. Never invent numbers.\n\n"
              f"QUESTION: {question}\n\nDATA (JSON): {json.dumps(data, ensure_ascii=False, default=str)[:12000]}")
    if _client is not None:
        resp = await client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}], temperature=0.1)
        return (resp.choices[0].message.content or "").strip()
    for client, model, name in chain:
        try:
            resp = await client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}],
                                                        temperature=0.1, max_tokens=900)
            txt = (resp.choices[0].message.content or "").strip()
            if txt:
                log.info("deep_reason answered by %s", name)
                return txt
        except Exception as e:
            _cooldown(name, e)
            log.warning("reasoning provider %s failed: %s", name, str(e)[:160])
    # no strong model free right now: the normal talking chain still answers
    resp = await _complete([{"role": "user", "content": prompt}], [], "reasoning")
    return (resp.choices[0].message.content or "").strip()


class Memory:
    """Short per-conversation memory (plain text turns only)."""

    def __init__(self, turns: int = 12) -> None:
        self._m: dict[str, deque] = defaultdict(lambda: deque(maxlen=turns))

    def get(self, key: str) -> list[dict]:
        return list(self._m[key])

    def add(self, key: str, role: str, content: str) -> None:
        self._m[key].append({"role": role, "content": content})


def _call_dict(c) -> dict:
    """Tool call to send back next turn. Gemini 3 needs its `extra_content` (thought signature) returned as-is."""
    d = {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
    extra = getattr(c, "extra_content", None)
    if extra is None and hasattr(c, "model_extra"):
        extra = (c.model_extra or {}).get("extra_content")
    if extra:
        d["extra_content"] = extra
    return d


async def run_agent(system: str, history: list[dict], user_text: str, tools: list[Tool],
                    max_steps: int = 6, agent_name: str = "agent") -> str:
    by_name = {t.name: t for t in tools}
    messages: list[dict] = [{"role": "system", "content": system}, *history,
                            {"role": "user", "content": user_text}]
    for _ in range(max_steps):
        resp = await _complete(messages, tools, agent_name)
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            return (msg.content or "").strip()
        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [_call_dict(c) for c in calls]})
        for c in calls:
            tool = by_name.get(c.function.name)
            try:
                args = json.loads(c.function.arguments or "{}")
                if tool is None:
                    raise ValueError(f"unknown tool {c.function.name}")
                import inspect as _insp
                sig = _insp.signature(tool.fn)
                if not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                    args = {k: v for k, v in (args or {}).items() if k in sig.parameters}
                result = await tool.fn(**args)
            except Exception as e:  # report tool errors back to the model instead of crashing
                log.exception("[%s] tool %s failed", agent_name, c.function.name)
                result = {"error": str(e)}
            log.info("[%s] %s(%s) -> %s", agent_name, c.function.name, c.function.arguments, str(result)[:300])
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)})
    return "Maaf kijiye, abhi samajh nahi paaya. Dobara boliye."
