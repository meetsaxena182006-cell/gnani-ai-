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


def get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(api_key=settings.llm_api_key or "missing", base_url=settings.llm_base_url)
    return _client


def set_client(client) -> None:
    """Tests inject a scripted fake client here."""
    global _client
    _client = client


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
    client = get_client()
    for _ in range(max_steps):
        resp = await client.chat.completions.create(
            model=settings.llm_model, messages=messages,
            tools=[t.schema() for t in tools] or None, temperature=0.2,
        )
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
