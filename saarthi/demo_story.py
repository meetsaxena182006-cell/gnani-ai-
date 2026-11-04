"""Fill the system with a SYNTHETIC day of operations so the office dashboard can be reviewed without real trucks.

    python -m saarthi.demo_story          # then: python -m saarthi.main  ->  http://localhost:8080/office

Everything here is fake: demo fleets, plates, drivers, parties and amounts. No LLM or speech calls are made.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from . import business, db, fastag, fatigue, geo, seed


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


async def story() -> None:
    seed.seed(demo=True)

    async def no_lookup(*a, **k):  # keep the story offline
        return []
    geo.nearby = no_lookup

    names = {"MW-T1": "Ramesh (demo)", "MW-T2": "Mahesh (demo)", "MW-T3": "Kishan (demo)", "SG-T1": "Suresh (demo)",
             "SG-T2": "Gurpreet (demo)"}
    for code, name in names.items():
        db.ex("UPDATE vehicles SET driver_name=? WHERE code=?", (name, code))

    # MW-T1: Udaipur -> Ahmedabad on NH48, crossing Paduna and Khandi Obari (FASTag auto-tolls)
    v1 = db.vehicle_by_code("MW-T1")
    t1 = db.active_trip(v1["id"])
    db.ex("UPDATE trips SET origin_lat=24.585, origin_lon=73.712, eway_bill='EWB-DEMO-1001', eway_valid_until=? WHERE id=?",
          ((datetime.now(timezone.utc) + timedelta(hours=30)).isoformat(timespec="minutes"), t1["id"]))
    for lat, lon in [(24.45, 73.70), (24.33, 73.69), (24.2714, 73.6738), (24.12, 73.65), (23.9936, 73.6279), (23.80, 73.45), (23.62, 73.255)]:
        await fatigue.location_update(v1["id"], lat, lon, 48)
    db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,litres,status,created_at,transcript) VALUES(?,?, 'diesel', 8740, 95, 'confirmed', ?, ?)",
          (v1["id"], t1["id"], _ago(hours=3), "bhaiya 8740 ka diesel daala 95 litre"))
    db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,status,created_at) VALUES(?,?, 'freight_advance', 10000, 'confirmed', ?)",
          (v1["id"], t1["id"], _ago(hours=9)))
    db.ex("INSERT INTO expenses(vehicle_id,trip_id,category,amount,note,status,created_at) VALUES(?,?, 'food', 240, 'dhaba', 'confirmed', ?)",
          (v1["id"], t1["id"], _ago(hours=2)))
    await fastag.reconcile({"amount": 435.0, "plaza": "PADUNA", "vehicle": v1["reg_label"]})
    business.ledger_add(v1["id"], "salary", 18000, "Nov salary"); business.ledger_add(v1["id"], "advance", 2000, "trip advance")

    # MW-T2: delivered last week at Surat after 9 h waiting; invoice overdue
    v2 = db.vehicle_by_code("MW-T2")
    t2 = db.start_trip(v2["id"], "Bhilwara", "Surat", 42000, "Shree Textiles (demo)")
    db.ex("INSERT INTO site_visits(vehicle_id,trip_id,site,arrived_at,left_at,waited_h,detention_amount,source) "
          "VALUES(?,?, 'unloading: Surat', ?, ?, 9.2, 1500, 'gps')", (v2["id"], t2["id"], _ago(days=8, hours=9), _ago(days=8)))
    db.ex("UPDATE trips SET delivered_at=?, status='closed' WHERE id=?", (_ago(days=8), t2["id"]))
    db.ex("INSERT INTO invoices(fleet_id,trip_id,party,freight,detention,advance_received,amount_due,issued_at,due_at,status,pod_file) "
          "VALUES(?,?,?,?,?,?,?,?,?, 'overdue', 'demo.jpg')",
          (v2["fleet_id"], t2["id"], "Shree Textiles (demo)", 42000, 1500, 15000, 28500, _ago(days=8),
           (date.today() - timedelta(days=4)).isoformat()))
    db.ex("UPDATE vehicles SET odometer_km=59620, last_service_km=40000, service_interval_km=20000 WHERE id=?", (v2["id"],))
    await fatigue.location_update(v2["id"], 24.60, 73.67, 0)
    business.ledger_add(v2["id"], "salary", 17000, "Nov salary"); business.ledger_add(v2["id"], "paid", 10000, "UPI")

    # MW-T3: pending challan
    v3 = db.vehicle_by_code("MW-T3")
    business.add_challan(v3["id"], "RJ-DEMO-7781", 2000, "No PUC (demo)", "2026-10-28")

    # SHREE fleet posts a load from Ahmedabad (a return load for MW-T1)
    shree = db.one("SELECT * FROM fleets WHERE code='SHREE'")
    db.ex("INSERT INTO loads(fleet_id,origin,destination,o_lat,o_lon,d_lat,d_lon,weight_t,vtype,freight,status,created_at) "
          "VALUES(?, 'Ahmedabad', 'Udaipur', 23.0225, 72.5714, 24.585, 73.712, 14, 'truck_10w', 16500, 'open', ?)",
          (shree["id"], db.now()))
    await business.return_load_check()
    print("Demo story ready. Run: python -m saarthi.main  ->  /office")


if __name__ == "__main__":
    asyncio.run(story())
