"""Fatigue check-ins: short yes/no prompts while driving; missed check-ins go to the CEO Agent.
Also tracks movement from Telegram live-location updates (no GPS hardware)."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from . import db, geo
from .agents import ceo
from .agents.common import ist_now
from .bus import bus, to_driver
from .config import settings
from .safety import is_moving

_pending: dict[int, datetime] = {}       # vehicle_id -> when the check-in was sent
_last_moving: dict[int, datetime] = {}
_live_since: dict[int, str] = {}
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


async def location_update(vid: int, lat: float, lon: float, speed_kmph: float | None = None) -> dict:
    """GPS fix from the phone: update position, then let the CEO watchdog look at it (tolls, stops, speed...)."""
    from . import watchdog
    before = db.vehicle(vid)
    after = update_location(vid, lat, lon, speed_kmph)
    try:
        await watchdog.on_location(before, after, after["last_speed_kmph"])
    except Exception:
        import logging
        logging.getLogger("saarthi.watchdog").exception("watchdog on_location failed")
    return after


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
    for v in db.q("SELECT * FROM vehicles"):
        if not v["driver_chat_id"] and v["id"] not in bus.live:
            continue
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
        live = v["id"] in bus.live
        if sent or not (live or (is_moving(v) and v["driving_since"])):
            continue
        start = _live_since.setdefault(v["id"], t_now.isoformat(timespec="seconds")) if live else None
        ref = max(filter(None, [v["driving_since"], v["last_fatigue_ack"], start]))
        if t_now - datetime.fromisoformat(ref) >= timedelta(minutes=_interval_min()):
            await _send_check(v)


async def _send_check(v: dict, retry: bool = False) -> None:
    _pending[v["id"]] = datetime.now(timezone.utc)
    hours = ""
    if v["driving_since"]:
        h = (datetime.now(timezone.utc) - datetime.fromisoformat(v["driving_since"])).total_seconds() / 3600
        hours = f" {h:.0f} ghante ho gaye chalate hue." if h >= 1 else ""
    if retry:
        text = "Boss? Sab theek hai na? Bas haan bol dijiye."
    else:
        text = random.choice([
            "Ram Ram boss! Sab theek? Tabiyat badhiya hai na?",
            "Boss, kaisa chal raha hai safar? Koi madad chahiye?",
            "Ustaad, sab okay? Neend to nahi aa rahi?",
        ]) + hours
        if hours and float(hours.split()[0]) >= 4:
            text += " Agle dhabe pe chai aur 15 minute aaram ho jaaye?"
    await bus.send(to_driver(v, "😴 " + text, speak=text, buttons=[("✅ Haan, theek hoon", "cb:fatigue:ok")]))
