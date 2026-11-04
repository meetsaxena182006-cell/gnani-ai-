"""The seven business features: detention, payments, driver ledger, e-way bill, service, challans, return loads."""
from datetime import date, datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from saarthi import business, db, fatigue, geo
from saarthi.agents import ceo, driver
from saarthi.bus import bus
from saarthi.config import settings

AHMEDABAD = (23.0225, 72.5714)


def mw1():
    return db.vehicle_by_code("MW-T1")


def owner_msgs(chat=1001):
    return [o.text for o in bus.sent if o.chat_id == chat]


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


# ---- 1. detention ----
def test_detention_amount_rule():
    assert business.detention_amount(3.5, 4, 1500) == 0
    assert business.detention_amount(7.3, 4, 1500) == 1500        # part of a day counts
    assert business.detention_amount(30, 4, 1500) == 3000


async def test_gps_detention_at_unloading_site(fresh):
    vid = mw1()["id"]
    await fatigue.location_update(vid, AHMEDABAD[0] + 0.005, AHMEDABAD[1], 0)     # arrives & waits at unloading point
    visit = db.one("SELECT * FROM site_visits")
    assert visit and visit["site"].startswith("unloading")
    db.ex("UPDATE site_visits SET arrived_at=? WHERE id=?", (ago(hours=7, minutes=20), visit["id"]))
    bus.sent.clear()
    await fatigue.location_update(vid, AHMEDABAD[0] + 0.06, AHMEDABAD[1], 30)      # leaves (> 2.5 km)
    v = db.one("SELECT * FROM site_visits")
    assert v["left_at"] and round(v["waited_h"]) == 7 and v["detention_amount"] == 1500
    assert any("Detention claim" in t and "₹1,500" in t for t in owner_msgs())


# ---- 2. payments ----
async def test_delivery_pod_invoice_reminder_payment(fresh):
    v = mw1(); t = db.active_trip(v["id"])
    db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,status,created_at) VALUES(?,?, 'freight_advance', 10000, 'confirmed', ?)",
          (v["id"], t["id"], db.now()))
    db.ex("INSERT INTO site_visits(vehicle_id,trip_id,site,arrived_at,left_at,waited_h,detention_amount) VALUES(?,?, 'unloading: x', ?, ?, 8, 1500)",
          (v["id"], t["id"], ago(hours=8), db.now()))
    r = await business.delivered(v, "Godown manager")
    assert "photo" in r["say"]
    inv = db.one("SELECT * FROM invoices")
    assert inv["amount_due"] == 38000 + 1500 - 10000 and inv["status"] == "awaiting_pod"
    assert any(a.get("type") == "pod" for o in bus.sent for a in o.actions)
    # POD upload from the phone
    settings.api_shared_secret = "s3cret"
    async with TestClient(TestServer(__import__("saarthi.web", fromlist=["build_app"]).build_app())) as c:
        import aiohttp
        fd = aiohttp.FormData(); fd.add_field("file", b"\xff\xd8fakejpeg", filename="pod.jpg", content_type="image/jpeg")
        res = await c.post(f"/api/pod?v=MW-T1&k={v['access_token']}", data=fd)
        assert (await res.json())["ok"]
        biz = await (await c.get("/api/business?fleet=MEWAR", headers={"X-Saarthi-Key": "s3cret"})).json()
        assert biz["receivables"]["total_receivable"] == 29500
    inv = db.one("SELECT * FROM invoices")
    assert inv["status"] == "due" and inv["pod_file"]
    assert any("Invoice ready" in t and "Detention" in t for t in owner_msgs())
    # overdue -> follow-up drafted for the owner to forward
    db.ex("UPDATE invoices SET due_at=? WHERE id=?", ((date.today() - timedelta(days=5)).isoformat(), inv["id"]))
    bus.sent.clear()
    await business.payment_reminders()
    assert any("Payment follow-up" in t and "5 din se overdue" in t for t in owner_msgs())
    # owner: "Demo Client se 20,000 aa gaye"
    res = business.record_payment(inv["fleet_id"], "Demo Client", 20000)
    assert res["applied"][0]["status"] == "part_paid"
    assert business.receivables(inv["fleet_id"])["total_receivable"] == 9500


# ---- 3. driver ledger ----
async def test_driver_ledger_and_voice_balance(fresh):
    v = mw1()
    await business.ledger_tick()                                   # bata for today's active trip
    await business.ledger_tick()                                   # not twice
    business.ledger_add(v["id"], "salary", 18000, "Nov")
    db.ex("INSERT INTO expenses(vehicle_id,category,amount,status,created_at) VALUES(?, 'advance_to_driver', 2000, 'pending', ?)",
          (v["id"], db.now()))
    await driver.confirm_all(db.vehicle(v["id"]))                  # advance confirmed -> ledger
    b = business.driver_balance(v["id"])
    assert b["earned"] == 18400 and b["advances"] == 2000 and b["balance_due_to_driver"] == 16400
    tools = {t.name: t for t in driver.make_tools(v["id"], driver.Turn())}
    r = await tools["my_earnings"].fn()
    assert "₹16,400" in r["say"]


# ---- 4. e-way bill ----
def test_eway_validity_rule():
    gen = datetime(2026, 11, 4, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert business.eway_validity(gen, 150).strftime("%d %H:%M") == "05 23:59"
    assert business.eway_validity(gen, 450).strftime("%d") == "07"


async def test_eway_expiring_before_eta_warns(fresh):
    v = mw1(); t = db.active_trip(v["id"])
    db.ex("UPDATE trips SET eway_bill='EWB123', eway_valid_until=? WHERE id=?",
          ((datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="minutes"), t["id"]))
    bus.sent.clear()
    await business.eway_check()
    assert any("E-way bill EWB123" in t and "Extend" in t for t in owner_msgs())
    assert any(o.vehicle_id == v["id"] and "e-way bill" in (o.speak or "") for o in bus.sent)


# ---- 5. service by km ----
async def test_service_due_by_gps_km(fresh):
    v = mw1()
    db.ex("UPDATE vehicles SET odometer_km=19700, last_service_km=0, service_interval_km=20000 WHERE id=?", (v["id"],))
    await fatigue.location_update(v["id"], 24.30, 73.68, 50)        # GPS km keeps adding to the odometer
    assert db.vehicle(v["id"])["odometer_km"] > 19700
    bus.sent.clear()
    await business.service_check()
    assert any("service due in" in t for t in owner_msgs())
    business.service_done(v["id"])
    bus.sent.clear()
    await business.service_check()
    assert not any("service due" in t for t in owner_msgs())


# ---- 6. e-challan ----
async def test_challans_manual_and_provider(fresh, monkeypatch):
    v = mw1()
    business.add_challan(v["id"], "RJ123", 1000, "overloading")
    business.add_challan(v["id"], "RJ123", 1000, "overloading")    # no duplicates
    assert db.one("SELECT COUNT(*) n FROM challans")["n"] == 1
    assert business._map({"challan_number": "X9", "fine": "500", "violation": "no PUC"})["amount"] == 500
    f = db.one("SELECT * FROM fleets WHERE code='MEWAR'")
    tools = {t.name: t for t in ceo._fleet_tools(f["id"])}
    rows = await tools["challans"].fn(mark_paid="RJ123")
    assert rows[0]["status"] == "paid"


# ---- 7. return loads ----
async def test_return_load_mesh(fresh, monkeypatch):
    async def gc(place):
        return {"name": place, "lat": AHMEDABAD[0], "lon": AHMEDABAD[1]} if "Ahmedabad" in place else {"name": place, "lat": 24.58, "lon": 73.71}
    monkeypatch.setattr(geo, "geocode", gc)
    shree = db.one("SELECT * FROM fleets WHERE code='SHREE'")
    r = await business.post_load(shree["id"], "Ahmedabad", "Udaipur", 30000, 12, "truck_10w")
    lid = r["load_id"]
    v = mw1()
    await fatigue.location_update(v["id"], 23.40, 72.80, 45)        # within 120 km of Ahmedabad
    bus.sent.clear()
    await business.return_load_check()
    msg = next(t for t in owner_msgs() if "Return load" in t)
    assert f"#{lid}" in msg and "est. profit" in msg
    res = await business.accept_load(lid, v)
    assert res["taken"] == lid
    assert any(o.chat_id == 2001 and "was taken" in o.text for o in bus.sent)  # poster informed
    assert (await business.accept_load(lid, db.vehicle_by_code("MW-T2")))["error"]
