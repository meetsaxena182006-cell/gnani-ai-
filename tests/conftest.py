import json
import os
from types import SimpleNamespace

os.environ.setdefault("SPEECH_MODE", "mock")
os.environ["DB_PATH"] = ":memory:"

import pytest

from saarthi import db, llm
from saarthi.bus import bus
from saarthi.config import settings


class FakeLLM:
    """Scripted OpenAI-compatible client. Each script item is either a str (final answer)
    or a list of (tool_name, args) tool calls."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, str):
            msg = SimpleNamespace(content=step, tool_calls=None)
        else:
            msg = SimpleNamespace(content="", tool_calls=[
                SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                for i, (n, a) in enumerate(step)])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.fixture
def fresh(monkeypatch):
    settings.db_path = ":memory:"
    settings.owner_bot_token = ""
    db.reset(":memory:")
    from saarthi import seed
    seed.seed(demo=True)
    # register synthetic chats
    db.ex("UPDATE fleets SET owner_chat_id=1001 WHERE code='MEWAR'")
    db.ex("UPDATE fleets SET owner_chat_id=2001 WHERE code='SHREE'")
    db.ex("UPDATE vehicles SET driver_chat_id=11, driver_name='Ramesh (demo)' WHERE code='MW-T1'")
    db.ex("UPDATE vehicles SET driver_chat_id=21, driver_name='Suresh (demo)' WHERE code='SG-T1'")
    db.ex("UPDATE vehicles SET driver_chat_id=12, driver_name='Mahesh (demo)' WHERE code='MW-T2'")
    bus.sent.clear()
    bus._sender = None
    # no network in tests
    async def no_nearby(*a, **k):
        return []
    monkeypatch.setattr("saarthi.geo.nearby", no_nearby)
    yield
    llm.set_client(None)


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _fresh_live_caches():
    """Each test starts with empty live-data caches (they are module-level for speed in production)."""
    from saarthi import geo, planner, tolls
    for c in (geo._route_cache, geo._along_cache, geo._geo_cache, geo._met_hourly_cache, geo._meteo_cache,
              planner._cum_cache, planner._sim_memo, tolls._on_route_cache):
        c.clear()
    geo._overpass_down_until = 0.0
    geo._meteo_down_until = 0.0
    try:
        from saarthi import fastlane
        fastlane._plan_memo.clear()
    except Exception:
        pass
    yield
