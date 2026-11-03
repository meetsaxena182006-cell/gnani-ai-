"""Outgoing message bus. Agents never talk to Telegram directly; they emit `Out` messages.
The Telegram app (or the CLI simulator) registers a sender. This also makes tests easy."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("saarthi.bus")


@dataclass
class Out:
    chat_id: int
    text: str
    channel: str = "driver"            # "driver" bot or "owner" bot
    speak: str | None = None           # text to voice with Timbre (None = no voice)
    buttons: list[tuple[str, str]] = field(default_factory=list)   # (label, "cb:..." or "https://..." / "tg://")
    contacts: list[tuple[str, str]] = field(default_factory=list)  # (name, phone) tappable to call
    location: tuple[float, float] | None = None


Sender = Callable[[Out], Awaitable[None]]


class Bus:
    def __init__(self) -> None:
        self._sender: Sender | None = None
        self.sent: list[Out] = []  # kept for tests / simulator

    def set_sender(self, sender: Sender) -> None:
        self._sender = sender

    async def send(self, out: Out) -> None:
        self.sent.append(out)
        if len(self.sent) > 500:
            self.sent = self.sent[-200:]
        if self._sender is None:
            log.info("[no sender] -> %s: %s", out.chat_id, out.text)
            return
        try:
            await self._sender(out)
        except Exception:
            log.exception("send failed to %s", out.chat_id)


bus = Bus()
