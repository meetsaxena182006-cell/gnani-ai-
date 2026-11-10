"""Cloudflare TURN credentials: cleaned, cached, merged with STUN, and safe when Cloudflare is down."""
import asyncio

import httpx

from saarthi import turn


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_client(calls, data):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append((url, headers, json))
            if isinstance(data, Exception):
                raise data
            return _Resp(data)
    return C


CF_REPLY = {"iceServers": [
    {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
    {"urls": ["turn:turn.cloudflare.com:3478?transport=udp", "turn:turn.cloudflare.com:53?transport=udp",
              "turns:turn.cloudflare.com:443?transport=tcp"],
     "username": "u1", "credential": "c1"}]}


def _setup(monkeypatch, data):
    calls = []
    monkeypatch.setenv("CF_TURN_KEY_ID", "key123")
    monkeypatch.setenv("CF_TURN_API_TOKEN", "tok456")
    monkeypatch.setattr(turn, "_cache", {"servers": None, "expires": 0.0})
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(calls, data))
    return calls


def test_without_cloudflare_settings_uses_stun(monkeypatch):
    monkeypatch.delenv("CF_TURN_KEY_ID", raising=False)
    monkeypatch.delenv("CF_TURN_API_TOKEN", raising=False)
    servers = asyncio.run(turn.ice_servers())
    assert servers and all("username" not in s for s in servers)


def test_turn_credentials_fetched_cleaned_and_cached(monkeypatch):
    calls = _setup(monkeypatch, CF_REPLY)
    servers = asyncio.run(turn.ice_servers())
    urls = [u for s in servers for u in s["urls"]]
    assert "turns:turn.cloudflare.com:443?transport=tcp" in urls
    assert not any(":53" in u for u in urls)             # browsers block port 53
    relay = next(s for s in servers if s.get("username"))
    assert relay["credential"] == "c1"
    url, headers, body = calls[0]
    assert "key123" in url and headers["Authorization"] == "Bearer tok456" and body == {"ttl": turn.TTL_S}
    asyncio.run(turn.ice_servers())
    assert len(calls) == 1                                # cached, not fetched per call


def test_single_object_reply_is_accepted(monkeypatch):
    _setup(monkeypatch, {"iceServers": {"urls": "turn:turn.cloudflare.com:3478", "username": "u", "credential": "c"}})
    servers = asyncio.run(turn.ice_servers())
    assert servers[0] == {"urls": ["turn:turn.cloudflare.com:3478"], "username": "u", "credential": "c"}


def test_cloudflare_down_falls_back_to_stun(monkeypatch):
    _setup(monkeypatch, httpx.ConnectError("offline"))
    servers = asyncio.run(turn.ice_servers())
    assert servers and all("username" not in s for s in servers)


def test_metered_style_url(monkeypatch):
    monkeypatch.delenv("CF_TURN_KEY_ID", raising=False)
    monkeypatch.delenv("CF_TURN_API_TOKEN", raising=False)
    monkeypatch.setenv("TURN_CREDENTIALS_URL", "https://demo.metered.live/api/v1/turn/credentials?apiKey=x")
    monkeypatch.setattr(turn, "_cache", {"servers": None, "expires": 0.0})
    reply = [{"urls": "stun:stun.relay.metered.ca:80"},
             {"urls": "turns:global.relay.metered.ca:443?transport=tcp", "username": "u", "credential": "c"}]

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            assert "apiKey=x" in url
            return _Resp(reply)
    monkeypatch.setattr(httpx, "AsyncClient", C)
    assert turn.configured()
    servers = asyncio.run(turn.ice_servers())
    assert any(s.get("username") == "u" for s in servers)


def test_static_username_password(monkeypatch):
    for k in ("CF_TURN_KEY_ID", "CF_TURN_API_TOKEN", "TURN_CREDENTIALS_URL", "TURN_URLS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TURN_USERNAME", "u9")
    monkeypatch.setenv("TURN_CREDENTIAL", "p9")
    assert turn.configured() and turn.problem() == ""
    servers = asyncio.run(turn.ice_servers())
    relay = servers[0]
    assert relay["username"] == "u9" and "turns:global.relay.metered.ca:443?transport=tcp" in relay["urls"]
