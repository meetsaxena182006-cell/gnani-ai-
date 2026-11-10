"""ICE servers for live voice (WebRTC), with optional Cloudflare TURN relay.

Phones on Jio/Airtel data sit behind carrier NAT, and a server on a laptop sits behind home NAT. Plain STUN
often cannot connect those two, so the voice call needs a TURN relay. Cloudflare Realtime TURN works well:
create a TURN key in the Cloudflare dashboard (Realtime -> TURN Server) and put its two values in .env:

    CF_TURN_KEY_ID=...
    CF_TURN_API_TOKEN=...

The key stays on the server. Short-lived credentials (24 h) are generated from it and refreshed before they
expire; both the phone and the server side of the call use them. Without these settings, ICE_SERVERS is used
as before (STUN only by default).
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from .config import settings

log = logging.getLogger("saarthi.turn")
CF_URL = "https://rtc.live.cloudflare.com/v1/turn/keys/{key}/credentials/generate-ice-servers"
TTL_S = 86400
REFRESH_BEFORE_S = 3600

_cache: dict = {"servers": None, "expires": 0.0}


def configured() -> bool:
    return bool(os.getenv("CF_TURN_KEY_ID") and os.getenv("CF_TURN_API_TOKEN"))


def _clean(servers: list[dict]) -> list[dict]:
    """Drop port-53 URLs (browsers block them, which only adds timeouts) and normalise the shape."""
    out = []
    for s in servers:
        urls = s.get("urls")
        urls = [urls] if isinstance(urls, str) else list(urls or [])
        urls = [u for u in urls if ":53?" not in u and not u.endswith(":53")]
        if not urls:
            continue
        item = {"urls": urls}
        if s.get("username"):
            item["username"] = s["username"]
            item["credential"] = s.get("credential", "")
        out.append(item)
    return out


async def _fetch_cloudflare() -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(CF_URL.format(key=os.environ["CF_TURN_KEY_ID"]),
                         headers={"Authorization": f"Bearer {os.environ['CF_TURN_API_TOKEN']}"},
                         json={"ttl": TTL_S})
        r.raise_for_status()
        data = r.json().get("iceServers")
    # the API has returned both a single object and a list over time
    return _clean(data if isinstance(data, list) else [data])


async def ice_servers() -> list[dict]:
    """ICE servers for a new call: Cloudflare TURN (cached) + the configured STUN servers."""
    base = _clean(settings.ice_servers)
    if not configured():
        return base
    now = time.time()
    if _cache["servers"] is None or now > _cache["expires"] - REFRESH_BEFORE_S:
        try:
            _cache["servers"] = await _fetch_cloudflare()
            _cache["expires"] = now + TTL_S
            log.info("Cloudflare TURN credentials refreshed")
        except Exception as e:  # keep the last good credentials if Cloudflare is briefly unreachable
            log.warning("Cloudflare TURN credentials failed: %s", e)
            if _cache["servers"] is None:
                return base
    have = {u for s in _cache["servers"] for u in s["urls"]}
    return _cache["servers"] + [s for s in base if not set(s["urls"]) & have]
