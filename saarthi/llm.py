"""Minimal tool-calling agent loop over any OpenAI-compatible endpoint (Gemini, Groq, local Evon
via vLLM, ...). Speech is always Gnani; this is only the reasoning step."""
from __future__ import annotations

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
        _providers = [(AsyncOpenAI(api_key=p["api_key"], base_url=p["base_url"], timeout=30), p["model"], p["name"])
                      for p in cfg]
    return _providers


def get_client():
    return providers()[0][0]


def set_client(client) -> None:
    """Tests inject a scripted fake client here."""
    global _client
    _client = client


async def _complete(messages: list[dict], tools: list["Tool"], agent_name: str):
    last_error = None
    for client, model, name in providers():
        try:
            return await client.chat.completions.create(
                model=model, messages=messages, tools=[t.schema() for t in tools] or None, temperature=0.2)
        except Exception as e:  # provider down / quota: try the next one
            last_error = e
            log.warning("[%s] LLM provider %s failed: %s; trying next", agent_name, name, e)
    raise last_error


class Memory:
    """Short per-conversation memory (plain text turns only)."""

    def __init__(self, turns: int = 12) -> None:
        self._m: dict[str, deque] = defaultdict(lambda: deque(maxlen=turns))

    def get(self, key: str) -> list[dict]:
        return list(self._m[key])

    def add(self, key: str, role: str, content: str) -> None:
        self._m[key].append({"role": role, "content": content})


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
        messages.append({
            "role": "assistant", "content": msg.content or "",
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments}}
                           for c in calls],
        })
        for c in calls:
            tool = by_name.get(c.function.name)
            try:
                args = json.loads(c.function.arguments or "{}")
                if tool is None:
                    raise ValueError(f"unknown tool {c.function.name}")
                result = await tool.fn(**args)
            except Exception as e:  # report tool errors back to the model instead of crashing
                log.exception("[%s] tool %s failed", agent_name, c.function.name)
                result = {"error": str(e)}
            log.info("[%s] %s(%s) -> %s", agent_name, c.function.name, c.function.arguments, str(result)[:300])
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)})
    return "Maaf kijiye, abhi samajh nahi paaya. Dobara boliye."
