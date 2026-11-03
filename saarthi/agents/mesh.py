"""Fleet Mesh: companions help each other like drivers on the same road do.

1. Truck A breaks down -> its companion asks every Saarthi companion within the radius (same fleet first,
   then other opted-in fleets).
2. Each nearby companion asks ITS driver by voice: "Aage ek truck ki gaadi kharab hai, madad kar sakte hain?"
3. The first driver to say "haan" is matched; A is told help is coming; both owners are informed.
4. Everyone else who was asked hears "madad mil gayi, shukriya". When A says the problem is solved, the
   helper and everyone asked is told too.
Privacy: no phone numbers are exchanged; only the two trucks' positions after the helper agrees.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .. import db, geo
from ..bus import bus, to_driver
from ..config import settings
from ..state import ask, questions
from . import ceo

FRESH_MIN = 30


def candidates(requester: dict, radius_km: float | None = None) -> list[dict]:
    radius_km = radius_km or settings.mesh_radius_km
    if requester.get("last_lat") is None:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=FRESH_MIN)).isoformat(timespec="seconds")
    rows = db.q(
        "SELECT v.*, f.name fleet_name FROM vehicles v JOIN fleets f ON f.id=v.fleet_id "
        "WHERE v.id<>? AND v.last_lat IS NOT NULL AND v.last_seen>=? AND (f.mesh_opt_in=1 OR f.id=?)",
        (requester["id"], cutoff, requester["fleet_id"]),
    )
    out = []
    for r in rows:
        if not r["driver_chat_id"] and r["id"] not in bus.live:
            continue  # unreachable companion
        d = geo.haversine_km(requester["last_lat"], requester["last_lon"], r["last_lat"], r["last_lon"])
        if d <= radius_km:
            r["distance_km"] = round(d, 1)
            r["same_fleet"] = r["fleet_id"] == requester["fleet_id"]
            out.append(r)
    out.sort(key=lambda r: (not r["same_fleet"], r["distance_km"]))
    return out[:5]


def _asked(req: dict) -> list[int]:
    return json.loads(req.get("asked") or "[]")


async def request_help(requester: dict, need: str) -> dict:
    if requester.get("last_lat") is None:
        return {"status": "no_location"}
    existing = db.one("SELECT * FROM mesh_requests WHERE requester_vehicle=? AND status IN ('open','matched')",
                      (requester["id"],))
    if existing:
        return {"status": "already_" + existing["status"], "request_id": existing["id"]}
    cands = candidates(requester)
    if not cands:
        return {"status": "none_nearby", "radius_km": settings.mesh_radius_km}
    rid = db.ex("INSERT INTO mesh_requests(requester_vehicle,need,lat,lon,created_at,asked) VALUES(?,?,?,?,?,?)",
                (requester["id"], need, requester["last_lat"], requester["last_lon"], db.now(),
                 json.dumps([c["id"] for c in cands])))
    for c in cands:
        db.log_agent(f"driver:{requester['code']}", f"driver:{c['code']}", "mesh_request", {"need": need, "id": rid})
        ask(c["id"], "mesh", request_id=rid)
        speak = (f"Boss, {c['distance_km']} kilometre door ek truck mushkil mein hai: {need}. "
                 f"Kya hum unki madad kar sakte hain? Haan ya nahi boliye.")
        await bus.send(to_driver(c, f"🤝 Paas mein ek truck ko madad chahiye ({c['distance_km']} km): {need}", speak=speak,
                                 buttons=[("✅ Haan, aata hoon", f"cb:mesh:accept:{rid}"), ("❌ Nahi", f"cb:mesh:decline:{rid}")]))
    return {"status": "asked", "request_id": rid, "asked": len(cands), "nearest_km": cands[0]["distance_km"]}


async def respond(helper: dict, request_id: int, accept: bool) -> str:
    req = db.one("SELECT * FROM mesh_requests WHERE id=?", (request_id,))
    if questions.get(helper["id"], {}).get("request_id") == request_id:
        questions.pop(helper["id"], None)
    if not req or req["status"] in ("solved", "closed"):
        return "Shukriya boss, unki problem solve ho chuki hai."
    if not accept:
        db.log_agent(f"driver:{helper['code']}", "mesh", "mesh_decline", {"id": request_id})
        return "Koi baat nahi boss, aap apna safar jaari rakhiye."
    if req["status"] != "open":
        return "Shukriya boss! Kisi aur truck ne pehle hi madad le li hai."
    db.ex("UPDATE mesh_requests SET status='matched', helper_vehicle=? WHERE id=? AND status='open'",
          (helper["id"], request_id))
    requester = db.vehicle(req["requester_vehicle"])
    dist = geo.haversine_km(req["lat"], req["lon"], helper["last_lat"], helper["last_lon"]) if helper.get("last_lat") else None
    away = f"{dist:.0f} kilometre door se " if dist else ""
    await bus.send(to_driver(
        requester, "✅ Madad aa rahi hai!",
        speak=f"Achhi khabar boss! Ek Saarthi truck {away}aapki madad ke liye aa raha hai. Wahin surakshit rahiye.",
        location=(helper["last_lat"], helper["last_lon"]) if helper.get("last_lat") else None))
    db.log_agent(f"driver:{helper['code']}", f"driver:{requester['code']}", "mesh_accept", {"id": request_id})
    # everyone else who was asked: help found
    for vid in _asked(req):
        if vid != helper["id"]:
            questions.pop(vid, None)
            other = db.vehicle(vid)
            await bus.send(to_driver(other, "🤝 Madad mil gayi.", speak="Boss, us truck ko madad mil gayi hai. Shukriya!"))
    await ceo.on_event(requester["id"], "peer_help_matched",
                       {"text": f"{requester['reg_label']} needed help ({req['need']}); a truck {away}accepted."})
    if helper["fleet_id"] != requester["fleet_id"]:
        await ceo.on_event(helper["id"], "peer_help_matched",
                           {"text": f"Your truck {helper['reg_label']} is helping another fleet's truck nearby."})
    return "Shukriya boss, aap asli hero ho! Truck ki location bhej di hai, dhyan se jaana."


async def close_requests(requester: dict, reason: str = "solved") -> None:
    """Requester's problem solved: tell the helper and everyone who was asked."""
    for req in db.q("SELECT * FROM mesh_requests WHERE requester_vehicle=? AND status IN ('open','matched')",
                    (requester["id"],)):
        db.ex("UPDATE mesh_requests SET status=? WHERE id=?", (reason, req["id"]))
        notify = set(_asked(req)) | ({req["helper_vehicle"]} if req["helper_vehicle"] else set())
        for vid in notify:
            questions.pop(vid, None)
            other = db.vehicle(vid)
            thanks = " Aapki madad ke liye bahut shukriya!" if vid == req["helper_vehicle"] else ""
            await bus.send(to_driver(other, "✅ Us truck ki problem solve ho gayi.",
                                     speak="Boss, us truck ki problem solve ho gayi hai." + thanks))


def requester_location(request_id: int) -> tuple[float, float] | None:
    req = db.one("SELECT lat, lon FROM mesh_requests WHERE id=?", (request_id,))
    return (req["lat"], req["lon"]) if req else None
