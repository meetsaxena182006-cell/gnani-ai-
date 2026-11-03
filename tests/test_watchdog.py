"""CEO watchdog: nothing is missed (tolls, long stops, offline, overspeed, diesel, route)."""
from datetime import datetime, timedelta, timezone

from saarthi import db, fatigue, tolls, watchdog
from saarthi.bus import bus


def mw1():
    return db.vehicle_by_code("MW-T1")


async def drive(points, speed=50):
    for lat, lon in points:
        await fatigue.location_update(mw1()["id"], lat, lon, speed)


def spoken_to(vid):
    return [o.speak or o.text for o in bus.sent if o.vehicle_id == vid]


def test_toll_data_and_class_rates():
    assert len(tolls.plazas()) > 500
    paduna = next(p for p in tolls.plazas() if p["name"] == "Paduna")
    assert tolls.expected_fee(paduna, "truck_10w") > tolls.expected_fee(paduna, "tempo_lcv") > 0
    route = [(24.58, 73.71), (24.27, 73.674), (23.99, 73.628), (23.62, 73.255)]
    r = tolls.on_route(route, "truck_10w")
    assert [p["name"] for p in r["plazas"]][:2] == ["Paduna", "Khandi Obari"]
    assert r["expected_total"] > 0


async def test_unreported_tolls_make_ceo_ask_driver(fresh):
    watchdog._stopped_since.clear()
    await drive([(24.40, 73.70), (24.2714, 73.6738), (24.10, 73.65), (23.9936, 73.6279), (23.90, 73.60)])
    crossings = db.q("SELECT plaza, status, expected_fee FROM toll_crossings ORDER BY id")
    assert [c["plaza"] for c in crossings] == ["Paduna", "Khandi Obari"]
    bus.sent.clear()
    await watchdog.tick()
    asked = spoken_to(mw1()["id"])
    assert any("Paduna aur Khandi Obari toll cross kiya" in s and "FASTag" in s for s in asked)
    # driver answers; once a toll entry is confirmed, crossings are cleared
    db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,status,created_at) VALUES(?,?, 'toll', 980, 'confirmed', ?)",
          (mw1()["id"], db.active_trip(mw1()["id"])["id"], db.now()))
    bus.sent.clear()
    await watchdog.tick()
    assert {c["status"] for c in db.q("SELECT status FROM toll_crossings")} == {"reported"}
    assert not any("toll" in s for s in spoken_to(mw1()["id"]))


async def test_same_plaza_not_counted_twice(fresh):
    await drive([(24.30, 73.68), (24.2714, 73.6738), (24.2715, 73.6739), (24.25, 73.67)])
    assert db.one("SELECT COUNT(*) n FROM toll_crossings")["n"] == 1


async def test_long_stop_asks_driver(fresh):
    vid = mw1()["id"]
    await fatigue.location_update(vid, 24.40, 73.70, 0)
    watchdog._stopped_since[vid] = datetime.now(timezone.utc) - timedelta(minutes=50)
    watchdog._asked_stop.discard(vid)
    bus.sent.clear()
    await watchdog.tick()
    assert any("minute se ruki hai" in s for s in spoken_to(vid))
    bus.sent.clear()
    await watchdog.tick()  # asked once per stop
    assert not any("ruki hai" in s for s in spoken_to(vid))


async def test_offline_truck_alerts_owner(fresh):
    vid = mw1()["id"]
    old = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat(timespec="seconds")
    db.ex("UPDATE vehicles SET last_seen=? WHERE id=?", (old, vid))
    watchdog._offline_alerted.discard(vid)
    bus.sent.clear()
    await watchdog.tick()
    assert any(o.chat_id == 1001 and "no location" in o.text for o in bus.sent)


async def test_overspeed_warning(fresh):
    watchdog._last_speed_warn.clear()
    bus.sent.clear()
    await drive([(24.45, 73.70)], speed=92)
    assert any("speed 92" in s for s in spoken_to(mw1()["id"]))


async def test_away_from_destination(fresh):
    watchdog._min_dest_km.clear(); watchdog._asked_away.clear()
    await drive([(24.30, 73.68)])               # closer to Ahmedabad
    bus.sent.clear()
    await drive([(24.62, 73.75), (24.75, 73.80)])  # heading back north, > 15 km further
    assert any("doori badh rahi" in s for s in spoken_to(mw1()["id"]))


async def test_low_diesel_efficiency_flagged(fresh):
    v = mw1()
    t = db.active_trip(v["id"])
    db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,litres,status,created_at) VALUES(?,?, 'diesel', 9000, 100, 'confirmed', ?)",
          (v["id"], t["id"], db.now()))
    db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,litres,status,created_at) VALUES(?,?, 'diesel', 9000, 100, 'confirmed', ?)",
          (v["id"], t["id"], db.now()))
    db.ex("UPDATE trips SET gps_km=200 WHERE id=?", (t["id"],))   # 200 km on 100 L = 2 km/L vs 3.5 expected
    bus.sent.clear()
    await watchdog.on_diesel(db.vehicle(v["id"]), 100, 9000)
    assert any(o.chat_id == 1001 and "Diesel check" in o.text for o in bus.sent)
