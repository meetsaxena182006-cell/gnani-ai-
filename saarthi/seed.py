"""Create SYNTHETIC demo fleets (fake names, fake plates, no real people).

    python -m saarthi.seed            # fresh DB with two demo fleets
    python -m saarthi.seed --demo     # also place trucks near Udaipur on NH48 with an active trip
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import base64
import os
import secrets
from urllib.parse import quote
from datetime import date, timedelta

from . import db
from .config import settings

FLEETS = [
    {"code": "MEWAR", "name": "Mewar Roadways (demo)", "diesel": 92.0, "vehicles": [
        ("MW-T1", "RJ00 DEMO 01", "truck_10w"), ("MW-T2", "RJ00 DEMO 02", "truck_6w"), ("MW-T3", "RJ00 DEMO 03", "tempo_lcv")]},
    {"code": "SHREE", "name": "Shree Ganesh Transport (demo)", "diesel": 92.0, "vehicles": [
        ("SG-T1", "GJ00 DEMO 11", "truck_10w"), ("SG-T2", "GJ00 DEMO 12", "trailer")]},
]


def access_token(code: str) -> str:
    """Same link every time when LINK_SECRET is set (the free cloud disk is wiped on every restart)."""
    secret = os.getenv("LINK_SECRET", "")
    if not secret:
        return secrets.token_urlsafe(9)
    mac = hmac.new(secret.encode(), code.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode()[:12]


def seed(demo: bool = False) -> None:
    if settings.db_path != ":memory:" and os.path.exists(settings.db_path):
        os.remove(settings.db_path)
    db.reset()
    today = date.today()
    for fl in FLEETS:
        fid = db.ex("INSERT INTO fleets(code,name,diesel_price) VALUES(?,?,?)", (fl["code"], fl["name"], fl["diesel"]))
        for i, (code, reg, vtype) in enumerate(fl["vehicles"]):
            vid = db.ex("INSERT INTO vehicles(fleet_id,code,reg_label,vtype,access_token) VALUES(?,?,?,?,?)",
                        (fid, code, reg, vtype, access_token(code)))
            docs = {"Insurance": 200 - 60 * i, "National Permit": 120, "Fitness": 45 - 20 * i, "PUC": 25 - 20 * i,
                    "Driver Licence": 600}
            for dt, days in docs.items():
                db.ex("INSERT INTO documents(vehicle_id,doc_type,expiry_date) VALUES(?,?,?)",
                      (vid, dt, (today + timedelta(days=days)).isoformat()))
    # demo KYC for the Saarthi network (made-up GSTIN-format numbers)
    db.ex("UPDATE fleets SET verified=1, gstin='08AAAAA0000A1Z5', rating=4.4, past_trips=36 WHERE code='MEWAR'")
    db.ex("UPDATE fleets SET verified=1, gstin='24AAAAA0000A1Z5', rating=4.6, past_trips=48 WHERE code='SHREE'")
    if demo:
        _demo_state()
    print(f"Seeded {settings.db_path}")
    for f in db.q("SELECT * FROM fleets"):
        vs = db.q("SELECT code, reg_label, access_token FROM vehicles WHERE fleet_id=?", (f["id"],))
        print(f"  {f['name']}: owner joins with  /start OWNER-{f['code']}")
        for v in vs:
            link = f"{settings.public_base_url}/driver?v={v['code']}&k={v['access_token']}"
            print(f"     {v['reg_label']}: companion link  {link}")
            print(f"        Android app setup:  saarthi://setup?url={quote(link, safe='')}   (Telegram fallback: /start {v['code']})")


def _demo_state() -> None:
    """Fake positions on NH48 near Udaipur (public highway coordinates, no real vehicles)."""
    from .fatigue import update_location

    spots = {"MW-T1": (24.5050, 73.7300), "SG-T1": (24.5402, 73.7010), "MW-T2": (24.6000, 73.6700)}
    for code, (lat, lon) in spots.items():
        v = db.vehicle_by_code(code)
        update_location(v["id"], lat, lon, speed_kmph=0)
    v1 = db.vehicle_by_code("MW-T1")
    t = db.start_trip(v1["id"], "Udaipur", "Ahmedabad", 38000, "Demo Client Pvt Ltd")
    db.ex("UPDATE trips SET dest_lat=23.0225, dest_lon=72.5714 WHERE id=?", (t["id"],))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    seed(ap.parse_args().demo)
