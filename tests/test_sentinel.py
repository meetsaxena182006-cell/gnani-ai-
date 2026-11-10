"""CEO Sentinel: proves nothing is missing, closes the loop on unanswered questions; companion memory."""
from datetime import date, datetime, timedelta, timezone

from saarthi import db, fatigue, sentinel, watchdog
from saarthi.agents import driver
from saarthi.bus import bus


def mw1():
    return db.vehicle_by_code("MW-T1")


def fid():
    return mw1()["fleet_id"]


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def check(audit, key):
    return next(c for c in audit["checks"] if c["key"] == key)


async def test_fleet_health_shape_and_toll_gap(fresh):
    db.ex("UPDATE vehicles SET last_lat=24.5, last_lon=73.7, last_seen=? WHERE code='MW-T1'", (db.now(),))
    h = sentinel.fleet_health(fid())
    assert h["trucks"] and 0 <= h["score_pct"] <= 100 and h["headline"]
    t = db.active_trip(mw1()["id"])
    db.ex("INSERT INTO toll_crossings(vehicle_id,trip_id,plaza,expected_fee,crossed_at,status) VALUES(?,?,?,?,?,?)",
          (mw1()["id"], t["id"], "Paduna", 400, db.now(), "unreported"))
    a = sentinel.audit_vehicle(mw1())
    assert check(a, "tolls")["status"] == "warn" and "Paduna" in check(a, "tolls")["detail"]
    assert any("Tolls" in x for x in a["attention"])
    assert not sentinel.fleet_health(fid())["all_clear"]


async def test_expired_eway_and_offline_gps_are_bad(fresh):
    t = db.active_trip(mw1()["id"])
    db.ex("UPDATE trips SET eway_valid_until=? WHERE id=?", (ago(60), t["id"]))
    db.ex("UPDATE vehicles SET last_seen=?, last_lat=24.5, last_lon=73.7 WHERE code='MW-T1'", (ago(90),))
    a = sentinel.audit_vehicle(mw1())
    assert check(a, "eway")["status"] == "bad"
    assert check(a, "gps")["status"] == "bad"
    assert a["status"] == "bad"


async def test_diesel_not_logged_for_more_than_a_tank(fresh):
    t = db.active_trip(mw1()["id"])
    db.ex("UPDATE trips SET gps_km=1200 WHERE id=?", (t["id"],))  # 10-wheeler: 300 L x 3.5 km/L = 1050 km
    assert check(sentinel.audit_vehicle(mw1()), "diesel")["status"] == "warn"
    db.ex("UPDATE vehicles SET odometer_km=5000 WHERE code='MW-T1'")
    db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,litres,status,created_at,odometer_km) "
          "VALUES(?,?, 'diesel', 9000, 100, 'confirmed', ?, 4900)", (mw1()["id"], t["id"], db.now()))
    assert check(sentinel.audit_vehicle(mw1()), "diesel")["status"] == "ok"


async def test_unanswered_toll_is_reasked_then_escalated_to_owner(fresh):
    vid = mw1()["id"]
    t = db.active_trip(vid)
    db.ex("INSERT INTO toll_crossings(vehicle_id,trip_id,plaza,expected_fee,crossed_at,status,asked_at) "
          "VALUES(?,?,?,?,?,?,?)", (vid, t["id"], "Paduna", 400, ago(80), "asked", ago(40)))
    bus.sent.clear()
    await sentinel.tick()
    to_driver = [o.speak or o.text for o in bus.sent if o.vehicle_id == vid]
    assert any("Paduna" in s and "kitna laga" in s for s in to_driver)
    assert db.one("SELECT asks FROM toll_crossings")["asks"] == 2
    # nothing again right away
    bus.sent.clear()
    await sentinel.tick()
    assert not [o for o in bus.sent if "Paduna" in (o.text or "")]
    # second reminder also ignored -> owner is told, once
    db.ex("UPDATE toll_crossings SET asked_at=?", (ago(40),))
    bus.sent.clear()
    await sentinel.tick()
    owner = [o.text for o in bus.sent if o.chat_id == 1001]
    assert any("Paduna" in s and "2 reminders" in s for s in owner)
    bus.sent.clear()
    await sentinel.tick()
    assert not [o for o in bus.sent if o.chat_id == 1001 and "Paduna" in o.text]


async def test_long_driving_gets_one_break_suggestion(fresh):
    db.ex("UPDATE vehicles SET driving_since=? WHERE code='MW-T1'", (ago(5 * 60),))
    bus.sent.clear()
    await sentinel.tick()
    said = [o.speak for o in bus.sent if o.vehicle_id == mw1()["id"] and o.speak]
    assert sum("aaram" in s for s in said) == 1
    assert all("speed" not in s.lower() for s in said)  # never pushed to hurry
    bus.sent.clear()
    await sentinel.tick()
    assert not [o for o in bus.sent if o.vehicle_id == mw1()["id"] and o.speak and "aaram" in o.speak]


async def test_expired_document_on_trip_alerts_owner_once(fresh):
    db.ex("INSERT INTO documents(vehicle_id, doc_type, expiry_date) VALUES(?, 'insurance', ?)",
          (mw1()["id"], (date.today() - timedelta(days=2)).isoformat()))
    bus.sent.clear()
    await sentinel.tick()
    await sentinel.tick()
    owner = [o.text for o in bus.sent if o.chat_id == 1001 and "Sentinel" in o.text]
    assert len([s for s in owner if "insurance" in s]) == 1


def test_companion_memory_dedups_and_reaches_prompt(fresh):
    vid = mw1()["id"]
    assert driver.remember_fact(vid, "Beti ka naam Pinky, 10th ka exam hai")
    assert not driver.remember_fact(vid, "beti ka naam pinky, 10th ka exam hai")
    for i in range(40):
        driver.remember_fact(vid, f"fact {i}")
    assert len(driver.memories(vid)) == driver.MEMORY_LIMIT
    driver.remember_fact(vid, "Kishore Kumar pasand hai")
    assert "Kishore Kumar" in driver.system_prompt(mw1(), moving=False)


async def test_newer_toll_question_does_not_delay_older_escalation(fresh):
    vid = mw1()["id"]
    t = db.active_trip(vid)
    # Paduna: already reminded twice, long ago. Vantada: asked just now for the first time.
    db.ex("INSERT INTO toll_crossings(vehicle_id,trip_id,plaza,expected_fee,crossed_at,status,asked_at,asks) "
          "VALUES(?,?,?,?,?,?,?,2)", (vid, t["id"], "Paduna", 400, ago(90), "asked", ago(40)))
    db.ex("INSERT INTO toll_crossings(vehicle_id,trip_id,plaza,expected_fee,crossed_at,status,asked_at,asks) "
          "VALUES(?,?,?,?,?,?,?,1)", (vid, t["id"], "Vantada", 120, ago(5), "asked", db.now()))
    bus.sent.clear()
    await sentinel.tick()
    owner = [o.text for o in bus.sent if o.chat_id == 1001]
    assert any("Paduna" in s and "Vantada" not in s for s in owner)
    assert not [o for o in bus.sent if o.vehicle_id == vid and "Vantada" in (o.speak or "")]  # its own clock
