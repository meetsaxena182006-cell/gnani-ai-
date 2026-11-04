"""FASTag automation + fuel pump stops."""
from datetime import datetime, timedelta, timezone

from saarthi import db, fastag, fatigue, geo, watchdog
from saarthi.bus import bus


def mw1():
    return db.vehicle_by_code("MW-T1")


async def through_paduna():
    for lat, lon in [(24.30, 73.68), (24.2714, 73.6738), (24.24, 73.67)]:
        await fatigue.location_update(mw1()["id"], lat, lon, 50)


async def test_fastag_toll_logged_automatically_without_asking(fresh):
    bus.sent.clear()
    await through_paduna()
    e = db.one("SELECT amount, note FROM expenses WHERE category='toll'")
    assert e and "FASTag Paduna (estimated)" in e["note"] and e["amount"] > 0
    await watchdog.tick()
    assert not any("toll cross kiya" in (o.speak or "") for o in bus.sent)   # driver not disturbed


async def test_statement_replaces_estimate_with_actual(fresh):
    await through_paduna()
    csv_text = ("Transaction Date,Vehicle Number,Plaza Name,Transaction Type,Amount\n"
                f"{datetime.now():%d-%m-%Y %H:%M},RJ00 DEMO 01,PADUNA TOLL PLAZA,Debit,415.00\n"
                f"{datetime.now():%d-%m-%Y},RJ00 DEMO 01,Recharge,Credit,2000\n")
    rows = fastag.parse_statement(csv_text)
    assert len(rows) == 1
    res = await fastag.reconcile(rows[0])
    assert res["status"] == "matched"
    e = db.one("SELECT amount, note FROM expenses WHERE category='toll'")
    assert e["amount"] == 415 and "(actual)" in e["note"]


async def test_deduction_without_gps_crossing_is_flagged(fresh):
    bus.sent.clear()
    d = fastag.parse_sms("Rs.560.00 debited from your FASTag for vehicle RJ00DEMO01 at Kishangarh toll plaza on 04-11-2026")
    assert d["amount"] == 560 and d["plaza"].lower().startswith("kishangarh")
    res = await fastag.reconcile(d, default_vehicle=mw1())
    assert res["status"] == "suspect"
    assert any(o.chat_id == 1001 and "dispute" in o.text for o in bus.sent)


async def test_fuel_stop_without_diesel_entry_asks_driver(fresh, monkeypatch):
    async def pump(kind, lat, lon, radius_m=15000, limit=5):
        return [{"name": "Demo Fuels NH48", "lat": lat, "lon": lon, "distance_km": 0.05}] if kind == "fuel" else []
    monkeypatch.setattr(geo, "nearby", pump)
    vid = mw1()["id"]
    watchdog._pump_stop.clear(); watchdog._pump_checked.clear(); watchdog._stopped_since.clear()
    await fatigue.location_update(vid, 24.45, 73.70, 0)          # stops at a pump
    watchdog._pump_stop[vid]["since"] = datetime.now(timezone.utc) - timedelta(minutes=12)
    bus.sent.clear()
    await fatigue.location_update(vid, 24.44, 73.70, 40)         # drives off, no diesel logged
    assert any("Demo Fuels NH48 pump pe" in (o.speak or "") and "Diesel bharwaya" in (o.speak or "")
               for o in bus.sent if o.vehicle_id == vid)


async def test_fuel_stop_with_diesel_logged_is_quiet(fresh, monkeypatch):
    async def pump(kind, lat, lon, radius_m=15000, limit=5):
        return [{"name": "Demo Fuels", "lat": lat, "lon": lon, "distance_km": 0.05}] if kind == "fuel" else []
    monkeypatch.setattr(geo, "nearby", pump)
    vid = mw1()["id"]
    watchdog._pump_stop.clear(); watchdog._pump_checked.clear(); watchdog._stopped_since.clear()
    await fatigue.location_update(vid, 24.45, 73.70, 0)
    since = datetime.now(timezone.utc) - timedelta(minutes=10)
    watchdog._pump_stop[vid]["since"] = since
    db.ex("INSERT INTO expenses(vehicle_id,category,amount,litres,status,created_at) VALUES(?, 'diesel', 9000, 100, 'confirmed', ?)",
          (vid, db.now()))
    bus.sent.clear()
    await fatigue.location_update(vid, 24.44, 73.70, 40)
    assert not any("pump pe" in (o.speak or "") for o in bus.sent)


async def test_fastag_endpoints(fresh):
    from aiohttp.test_utils import TestClient, TestServer
    from saarthi.config import settings
    from saarthi.web import build_app
    await through_paduna()
    settings.api_shared_secret = "s3cret"
    H = {"X-Saarthi-Key": "s3cret"}
    async with TestClient(TestServer(build_app())) as c:
        r = await c.post("/api/fastag/statement", data="Date,Vehicle No,Plaza,Amount\n04-11-2026,RJ00 DEMO 01,Paduna,420\n",
                         headers={**H, "Content-Type": "text/csv"})
        assert (await r.json())["matched"] == 1
        r = await c.post("/api/fastag/sms", json={"text": "INR 300 debited from FASTag RJ00DEMO01 at Ajmer plaza."}, headers=H)
        assert (await r.json())["status"] == "suspect"
        assert (await c.post("/api/fastag/sms", json={"text": "hi"})).status == 401
