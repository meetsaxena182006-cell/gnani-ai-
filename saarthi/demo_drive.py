"""A SIMULATED demo truck driving NH48 Udaipur -> Ahmedabad, again and again (clearly labelled "demo").

It crosses the real NHAI toll plazas at Paduna and Khandi Obari, so the CEO watchdog asks the driver
"kitna toll laga?" and the Sentinel follows up. Whoever opens that truck's driver link plays the driver and
can answer by voice or by typing. Phone GPS is ignored for this truck so the simulated route stays in charge.

Used by the review page (scripts/review_demo.py) and, with DEMO_DRIVE=1, by the full system.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from . import db, fatigue

WAYPOINTS = [(24.585, 73.712), (24.45, 73.70), (24.33, 73.69), (24.2714, 73.6738), (24.12, 73.65),
             (23.9936, 73.6279), (23.80, 73.45), (23.62, 73.255), (23.41, 72.83), (23.0225, 72.5714)]
STEP_S = 5
SIM_CODE = "MW-T3"
SIM_IDS: set[int] = set()   # vehicles driven by the simulation (their phone GPS is ignored)


def path(step_km: float = 2.5) -> list[tuple[float, float]]:
    from .geo import haversine_km
    pts = []
    for (a, b), (c, d) in zip(WAYPOINTS, WAYPOINTS[1:]):
        n = max(1, int(haversine_km(a, b, c, d) / step_km))
        pts += [(a + (c - a) * i / n, b + (d - b) * i / n) for i in range(n)]
    return pts + [WAYPOINTS[-1]]


def is_simulated(vid: int) -> bool:
    return vid in SIM_IDS


async def drive_forever(code: str = SIM_CODE) -> None:
    pts = path()
    v = db.vehicle_by_code(code)
    SIM_IDS.add(v["id"])
    db.ex("UPDATE vehicles SET driver_name=COALESCE(driver_name, 'Kishan (demo)') WHERE id=?", (v["id"],))
    while True:
        v = db.vehicle_by_code(code)
        old = db.active_trip(v["id"])
        if old:
            db.ex("UPDATE trips SET status='delivered' WHERE id=?", (old["id"],))
        db.ex("DELETE FROM toll_crossings WHERE vehicle_id=?", (v["id"],))
        db.ex("UPDATE vehicles SET fastag=0, driving_since=NULL WHERE id=?", (v["id"],))
        t = db.start_trip(v["id"], "Udaipur", "Ahmedabad", 31000, "Demo Cement Co. (demo)")
        db.ex("UPDATE trips SET dest_lat=23.0225, dest_lon=72.5714, origin_lat=24.585, origin_lon=73.712, "
              "eway_bill='EWB-DEMO-2002', eway_valid_until=? WHERE id=?",
              ((datetime.now(timezone.utc) + timedelta(hours=30)).isoformat(timespec="minutes"), t["id"]))
        db.log_agent("system", "review", "demo_lap", {"text": f"Demo truck {v['reg_label']} starts Udaipur → Ahmedabad"})
        for lat, lon in pts:
            await fatigue.location_update(v["id"], lat, lon, 52)
            await asyncio.sleep(STEP_S)
        await fatigue.location_update(v["id"], *WAYPOINTS[-1], 0)
        await asyncio.sleep(180)  # parked at Ahmedabad, then the next lap
