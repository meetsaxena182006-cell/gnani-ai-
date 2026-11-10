"""ICE servers for live voice (WebRTC), with optional Cloudflare TURN relay.

Phones on Jio/Airtel data sit behind carrier NAT, and a server on a laptop sits behind home NAT. Plain STUN
often cannot connect those two, so the voice call needs a TURN relay. Cloudflare Realtime TURN works well:
create a TURN key in the Cloudflare dashboard (Realtime -> TURN Server) and put its two values in .env:

    CF_TURN_KEY_ID=...
    CF_TURN_API_TOKEN=...

The key stays on the server. Short-lived credentials (24 h) are generated from it and refreshed before they
expire; both the phone and the server side of the call use them. Without these settings, ICE_SERVERS is used
as before (STUN only by default).

Free alternative without a card: Metered (https://www.metered.ca/stun-turn, free 20 GB/month). Create an app,
then set the credentials link it shows, with your API key:

    TURN_CREDENTIALS_URL=https://<your-app>.metered.live/api/v1/turn/credentials?apiKey=<key>

Any URL that returns a JSON list of ICE servers (or {"iceServers": [...]}) works.
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


def _cloudflare() -> bool:
    return bool(os.getenv("CF_TURN_KEY_ID") and os.getenv("CF_TURN_API_TOKEN"))


def _url() -> str:
    return os.getenv("TURN_CREDENTIALS_URL", "").strip().strip('"').strip("'")


METERED_URLS = ("turn:global.relay.metered.ca:80,turn:global.relay.metered.ca:80?transport=tcp,"
                "turn:global.relay.metered.ca:443,turns:global.relay.metered.ca:443?transport=tcp")


def _static() -> list[dict]:
    """Fixed TURN username/password (e.g. Metered's 'Show credentials' box): TURN_USERNAME + TURN_CREDENTIAL,
    TURN_URLS optional (defaults to Metered's global relay)."""
    user, cred = os.getenv("TURN_USERNAME", "").strip(), os.getenv("TURN_CREDENTIAL", "").strip()
    if not (user and cred):
        return []
    urls = [u.strip() for u in os.getenv("TURN_URLS", METERED_URLS).split(",") if u.strip()]
    return _clean([{"urls": urls, "username": user, "credential": cred}])


def configured() -> bool:
    return bool(_static()) or _cloudflare() or ("/turn/credentials" in _url() and "apikey=" in _url().lower() and _url().startswith("https://"))


def problem() -> str:
    """Plain-language reason TURN is off (shown on the links page); never shows the key."""
    if configured():
        return ""
    u = _url()
    if not u:
        return "not set on the server: add TURN_USERNAME + TURN_CREDENTIAL (or TURN_CREDENTIALS_URL)"
    if "dashboard.metered.ca" in u:
        return "TURN_CREDENTIALS_URL is the Metered dashboard page; it must be the API link (…metered.live/api/v1/turn/credentials?apiKey=…)"
    return "TURN_CREDENTIALS_URL does not look like …metered.live/api/v1/turn/credentials?apiKey=…"


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


async def _fetch_url() -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(_url())
        r.raise_for_status()
        data = r.json()
    if isinstance(data, dict):
        data = data.get("iceServers") or data.get("ice_servers") or []
    return _clean(data if isinstance(data, list) else [data])


async def ice_servers() -> list[dict]:
    """ICE servers for a new call: Cloudflare TURN (cached) + the configured STUN servers."""
    base = _clean(settings.ice_servers)
    if _static():
        return _static() + base
    if not configured():
        return base
    now = time.time()
    if _cache["servers"] is None or now > _cache["expires"] - REFRESH_BEFORE_S:
        try:
            _cache["servers"] = await (_fetch_cloudflare() if _cloudflare() else _fetch_url())
            _cache["expires"] = now + TTL_S
            log.info("TURN credentials refreshed (%s)", "Cloudflare" if _cloudflare() else "TURN_CREDENTIALS_URL")
        except Exception as e:  # keep the last good credentials if the provider is briefly unreachable
            log.warning("TURN credentials failed: %s", type(e).__name__)
            if _cache["servers"] is None:
                return base
    have = {u for s in _cache["servers"] for u in s["urls"]}
    return _cache["servers"] + [s for s in base if not set(s["urls"]) & have]
