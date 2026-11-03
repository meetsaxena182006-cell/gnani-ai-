"""Fatigue check-ins: short yes/no prompts while driving; missed check-ins go to the CEO Agent.
Also tracks movement from Telegram live-location updates (no GPS hardware)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db, geo
from .agents import ceo
from .agents.common import ist_now
from .bus import Out, bus
from .config import settings
from .safety import is_moving

_pending: dict[int, datetime] = {}       # vehicle_id -> when the check-in was sent
_last_moving: dict[int, datetime] = {}
REST_RESET_MIN = 15
ANSWER_WINDOW_MIN = 10


def update_location(vid: int, lat: float, lon: float, speed_kmph: float | None = None) -> dict:
    v = db.vehicle(vid)
    t_now = datetime.now(timezone.utc)
    if speed_kmph is None and v["last_lat"] is not None and v["last_seen"]:
        dt_h = (t_now - datetime.fromisoformat(v["last_seen"])).total_seconds() / 3600
        if dt_h > 0:
            speed_kmph = geo.haversine_km(v["last_lat"], v["last_lon"], lat, lon) / dt_h
            speed_kmph = min(speed_kmph, 120.0)  # ignore GPS jumps
    driving_since = v["driving_since"]
    if speed_kmph is not None and speed_kmph >= settings.moving_speed_kmph:
        _last_moving[vid] = t_now
        if not driving_since:
            driving_since = t_now.isoformat(timespec="seconds")
    else:
        last = _last_moving.get(vid)
        if driving_since and (last is None or t_now - last > timedelta(minutes=REST_RESET_MIN)):
            driving_since = None  # a real rest resets the driving clock
    db.ex("UPDATE vehicles SET last_lat=?, last_lon=?, last_speed_kmph=?, last_seen=?, driving_since=? WHERE id=?",
          (lat, lon, speed_kmph, t_now.isoformat(timespec="seconds"), driving_since, vid))
    return db.vehicle(vid)


def ack_if_pending(vid: int) -> bool:
    if vid in _pending:
        _pending.pop(vid)
        db.ex("UPDATE vehicles SET last_fatigue_ack=?, missed_checks=0 WHERE id=?", (db.now(), vid))
        return True
    return False


def _interval_min() -> int:
    h = ist_now().hour
    return min(60, settings.fatigue_interval_min) if 0 <= h < 5 else settings.fatigue_interval_min


async def tick() -> None:
    """Run every few minutes."""
    t_now = datetime.now(timezone.utc)
    for v in db.q("SELECT * FROM vehicles WHERE driver_chat_id IS NOT NULL"):
        sent = _pending.get(v["id"])
        if sent and t_now - sent > timedelta(minutes=ANSWER_WINDOW_MIN):
            missed = (v["missed_checks"] or 0) + 1
            db.ex("UPDATE vehicles SET missed_checks=? WHERE id=?", (missed, v["id"]))
            _pending.pop(v["id"], None)
            if missed >= 2:
                await ceo.on_event(v["id"], "fatigue_missed", {"missed": missed})
                db.ex("UPDATE vehicles SET missed_checks=0 WHERE id=?", (v["id"],))
            else:
                await _send_check(v, retry=True)
            continue
        if sent or not is_moving(v) or not v["driving_since"]:
            continue
        ref = max(filter(None, [v["driving_since"], v["last_fatigue_ack"]]))
        if t_now - datetime.fromisoformat(ref) >= timedelta(minutes=_interval_min()):
            await _send_check(v)


async def _send_check(v: dict, retry: bool = False) -> None:
    _pending[v["id"]] = datetime.now(timezone.utc)
    hours = ""
    if v["driving_since"]:
        h = (datetime.now(timezone.utc) - datetime.fromisoformat(v["driving_since"])).total_seconds() / 3600
        hours = f" {h:.0f} ghante se chala rahe ho."
    text = ("Dobara pooch raha hoon: " if retry else "") + f"Sab theek hai?{hours} Haan boliye ya button dabaiye."
    if not retry and hours and float(hours.split()[0]) >= 4:
        text += " Agle dhabe pe 15 minute aaram kar lijiye."
    await bus.send(Out(chat_id=v["driver_chat_id"], text="😴 " + text, speak=text,
                       buttons=[("✅ Haan, theek hoon", "cb:fatigue:ok")]))
