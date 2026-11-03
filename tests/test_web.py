from aiohttp.test_utils import TestClient, TestServer

from saarthi import db
from saarthi.config import settings
from saarthi.web import build_app


async def test_tracking_and_api_auth(fresh):
    t = db.active_trip(db.vehicle_by_code("MW-T1")["id"])
    async with TestClient(TestServer(build_app())) as c:
        r = await c.get(f"/track/{t['track_token']}")
        assert r.status == 200 and "Udaipur" in await r.text()
        js = await (await c.get(f"/track/{t['track_token']}.json")).json()
        assert js["lat"] == 24.505
        assert (await c.get("/track/nope.json")).status == 404
        settings.api_shared_secret = "s3cret"
        assert (await c.get("/api/digest")).status == 401
        r = await c.get("/api/digest", headers={"X-Saarthi-Key": "s3cret"})
        assert r.status == 200 and len(await r.json()) == 2
