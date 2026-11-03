"""Fleet Mesh: a Driver Agent in trouble asks nearby Driver Agents (any opted-in fleet) for help.
Privacy: no phone numbers are shared; only the approximate location of the two trucks after the
helper accepts. Each helper driver answers by voice/button; the first 'yes' wins."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import db, geo
from ..bus import Out, bus
from ..config import settings
from . import ceo

FRESH_MIN = 30


def candidates(requester: dict, radius_km: float | None = None) -> list[dict]:
    radius_km = radius_km or settings.mesh_radius_km
    if requester.get("last_lat") is None:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=FRESH_MIN)).isoformat(timespec="seconds")
    rows = db.q(
        "SELECT v.*, f.name fleet_name FROM vehicles v JOIN fleets f ON f.id=v.fleet_id "
        "WHERE v.id<>? AND v.driver_chat_id IS NOT NULL AND v.last_lat IS NOT NULL AND v.last_seen>=? "
        "AND (f.mesh_opt_in=1 OR f.id=?)",
        (requester["id"], cutoff, requester["fleet_id"]),
    )
    out = []
    for r in rows:
        d = geo.haversine_km(requester["last_lat"], requester["last_lon"], r["last_lat"], r["last_lon"])
        if d <= radius_km:
            r["distance_km"] = round(d, 1)
            r["same_fleet"] = r["fleet_id"] == requester["fleet_id"]
            out.append(r)
    out.sort(key=lambda r: (not r["same_fleet"], r["distance_km"]))
    return out[:5]


async def request_help(requester: dict, need: str) -> dict:
    cands = candidates(requester)
    if requester.get("last_lat") is None:
        return {"status": "no_location", "message": "Location share karo, tab paas ke truck dhoondh sakta hoon."}
    if not cands:
        return {"status": "none_nearby", "message": f"{int(settings.mesh_radius_km)} km mein koi Saarthi truck nahi mila."}
    rid = db.ex("INSERT INTO mesh_requests(requester_vehicle,need,lat,lon,created_at) VALUES(?,?,?,?,?)",
                (requester["id"], need, requester["last_lat"], requester["last_lon"], db.now()))
    for c in cands:
        db.log_agent(f"driver:{requester['code']}", f"driver:{c['code']}", "mesh_request", {"need": need, "id": rid})
        await bus.send(Out(
            chat_id=c["driver_chat_id"],
            text=f"🤝 Paas mein ek truck ko madad chahiye ({c['distance_km']} km door): {need}\nMadad kar sakte ho?",
            speak=f"Paas mein ek truck ko madad chahiye, {c['distance_km']} kilometre door. {need}. Madad kar sakte ho?",
            buttons=[("✅ Haan, aata hoon", f"cb:mesh:accept:{rid}"), ("❌ Nahi", f"cb:mesh:decline:{rid}")],
        ))
    return {"status": "asked", "request_id": rid, "asked": len(cands),
            "nearest_km": cands[0]["distance_km"]}


async def respond(helper: dict, request_id: int, accept: bool) -> str:
    req = db.one("SELECT * FROM mesh_requests WHERE id=?", (request_id,))
    if not req:
        return "Yeh request ab nahi hai."
    if not accept:
        db.log_agent(f"driver:{helper['code']}", "mesh", "mesh_decline", {"id": request_id})
        return "Theek hai, koi baat nahi."
    if req["status"] != "open":
        return "Shukriya! Kisi aur ne pehle hi madad le li hai."
    db.ex("UPDATE mesh_requests SET status='matched', helper_vehicle=? WHERE id=? AND status='open'",
          (helper["id"], request_id))
    requester = db.vehicle(req["requester_vehicle"])
    dist = geo.haversine_km(req["lat"], req["lon"], helper["last_lat"], helper["last_lon"]) if helper.get("last_lat") else None
    # tell the requester; share helper's position
    await bus.send(Out(
        chat_id=requester["driver_chat_id"],
        text=f"✅ Madad aa rahi hai! Ek truck" + (f" ({dist:.1f} km door)" if dist else "") + f" aapki madad ke liye aa raha hai.",
        speak="Madad aa rahi hai. Paas ka ek truck aapki taraf aa raha hai.",
        location=(helper["last_lat"], helper["last_lon"]) if helper.get("last_lat") else None,
    ))
    db.log_agent(f"driver:{helper['code']}", f"driver:{requester['code']}", "mesh_accept", {"id": request_id})
    text = f"{requester['reg_label']} needed help ({req['need']}); a truck {('%.1f km' % dist) if dist else ''} away accepted."
    await ceo.on_event(requester["id"], "peer_help_matched", {"text": text})
    if helper["fleet_id"] != requester["fleet_id"]:
        await ceo.on_event(helper["id"], "peer_help_matched", {"text": f"Your truck {helper['reg_label']} is helping another fleet's truck nearby."})
    return f"Shukriya! Truck ki location bhej raha hoon. Dhyan se jaana."


def requester_location(request_id: int) -> tuple[float, float] | None:
    req = db.one("SELECT lat, lon FROM mesh_requests WHERE id=?", (request_id,))
    return (req["lat"], req["lon"]) if req else None
