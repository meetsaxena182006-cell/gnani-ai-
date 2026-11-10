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
    vehicle_id: int | None = None      # set for driver messages: delivered to the live voice companion if connected
    actions: list[dict] = field(default_factory=list)  # client actions for the phone app, e.g. {"type": "dial", "phone": "112"}


def to_driver(v: dict, text: str, speak: str | None = None, **kw) -> Out:
    """Message for a driver: live voice companion first, Telegram as fallback."""
    return Out(chat_id=v.get("driver_chat_id") or 0, text=text, speak=speak, vehicle_id=v["id"], **kw)


Sender = Callable[[Out], Awaitable[None]]


class LiveTarget:  # implemented by companion.LiveSession
    async def deliver(self, out: Out) -> None: ...


class Bus:
    def __init__(self) -> None:
        self._sender: Sender | None = None
        self.live: dict[int, "LiveTarget"] = {}  # vehicle_id -> live companion session
        self.sent: list[Out] = []  # kept for tests / simulator
        self._recorder: Sender | None = None  # e.g. the web inbox: sees every message, however it is delivered

    def set_sender(self, sender: Sender) -> None:
        self._sender = sender

    def set_recorder(self, recorder: Sender | None) -> None:
        self._recorder = recorder

    async def send(self, out: Out) -> None:
        self.sent.append(out)
        if len(self.sent) > 500:
            self.sent = self.sent[-200:]
        if self._recorder is not None:
            try:
                await self._recorder(out)
            except Exception:
                log.exception("recorder failed")
        live = self.live.get(out.vehicle_id) if out.vehicle_id else None
        if live is not None:
            try:
                await live.deliver(out)
                return
            except Exception:
                log.exception("live delivery failed; falling back")
        if self._sender is None or not out.chat_id:
            log.info("[no sender] -> %s: %s", out.chat_id, out.text)
            return
        try:
            await self._sender(out)
        except Exception:
            log.exception("send failed to %s", out.chat_id)


bus = Bus()
