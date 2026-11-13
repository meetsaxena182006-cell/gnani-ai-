"""EMPTY-TRUCK AGENT (backhaul matching, agent to agent across fleets).

When a truck becomes empty after a delivery, its agent looks for the nearest work on the Saarthi network: another
fleet's overflow load (their own truck is busy or broke down) or an urgent requirement. Before anything is booked it
checks BOTH sides:

  carrier (our truck)  - papers valid for the whole new trip: Insurance, National Permit, Fitness, PUC, Driver Licence
                       - truck type and payload (no overloading)
  poster (other fleet) - legitimacy: KYC verified, GSTIN format, completed trips on the network, rating,
                         overdue payments to other fleets

Only loads that pass go through; the most profitable safe one is offered to the poster fleet's CEO agent, which
auto-accepts for verified carriers. Then the trip is booked and the driver + both owners are told. Result: fewer
empty kilometres, more paid work for small fleets.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from .. import business, costing, db, geo
from ..bus import bus, to_driver
from .common import inr, mask_reg

log = logging.getLogger("saarthi.backhaul")

SEARCH_KM = 80
REQUIRED_DOCS = ("Insurance", "National Permit", "Fitness", "PUC", "Driver Licence")
GSTIN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def _trip_days(load: dict) -> float:
    km = geo.haversine_km(load["o_lat"], load["o_lon"], load["d_lat"], load["d_lon"]) * 1.25
    return km / 400 + 1          # ~400 km/day + a day of loading/unloading


def check_papers(v: dict, load: dict) -> dict:
    """Carrier compliance for this load: every required paper valid until the trip is over."""
    need_until = date.today() + timedelta(days=_trip_days(load))
    docs = {d["doc_type"]: d["expiry_date"] for d in db.q("SELECT * FROM documents WHERE vehicle_id=?", (v["id"],))}
    problems, ok = [], []
    for dt in REQUIRED_DOCS:
        exp = docs.get(dt)
        if not exp:
            problems.append(f"{dt} missing")
        elif date.fromisoformat(exp[:10]) < date.today():
            problems.append(f"{dt} expired")
        elif date.fromisoformat(exp[:10]) < need_until:
            problems.append(f"{dt} expires during the trip ({exp[:10]})")
        else:
            ok.append(dt)
    prof = costing.PROFILES.get(v["vtype"])
    if load.get("weight_t") and prof and load["weight_t"] > prof.payload_tonnes:
        problems.append(f"load {load['weight_t']:g} t > payload {prof.payload_tonnes:g} t (overloading)")
    if load.get("vtype") and load["vtype"] != v["vtype"]:
        problems.append(f"needs {load['vtype']}, truck is {v['vtype']}")
    return {"pass": not problems, "valid": ok, "problems": problems}


def check_poster(fleet_id: int) -> dict:
    """Is the fleet posting the load legitimate? KYC, GSTIN, track record, rating, payment behaviour."""
    f = db.fleet(fleet_id)
    score, notes = 0, []
    if f.get("verified"):
        score += 40; notes.append("KYC verified")
    else:
        notes.append("KYC not verified")
    if f.get("gstin") and GSTIN.match(f["gstin"]):
        score += 20; notes.append("GSTIN valid format")
    else:
        notes.append("no valid GSTIN")
    done = db.one("SELECT COUNT(*) c FROM trips t JOIN vehicles v ON v.id=t.vehicle_id WHERE v.fleet_id=? AND "
                  "t.delivered_at IS NOT NULL", (fleet_id,))["c"] + (f.get("past_trips") or 0)
    if done >= 10:
        score += 20; notes.append(f"{done} trips completed on network")
    elif done:
        score += 10; notes.append(f"{done} trips completed")
    else:
        notes.append("no track record")
    rating = f.get("rating") or 0
    if rating >= 4:
        score += 10; notes.append(f"rating {rating:g}/5")
    overdue = f.get("overdue_payments") or 0
    if overdue:
        score -= 30; notes.append(f"{overdue} overdue payment(s) to other fleets")
    else:
        score += 10; notes.append("no overdue payments")
    return {"pass": score >= 60, "score": score, "notes": notes, "fleet": f["name"]}


def candidates(v: dict, lat: float, lon: float) -> list[dict]:
    """Every open load of another fleet near the empty truck, each with papers / legitimacy / profit checks."""
    out = []
    for l in business.loads_near(lat, lon, exclude_fleet=v["fleet_id"], radius_km=SEARCH_KM):
        papers = check_papers(v, l)
        poster = check_poster(l["fleet_id"])
        profit = business.load_profit(l, v)
        reason = None
        if not poster["pass"]:
            reason = "poster not trustworthy: " + ", ".join(n for n in poster["notes"] if "not" in n or "no " in n or "overdue" in n)
        elif not papers["pass"]:
            reason = "papers/capacity: " + "; ".join(papers["problems"])
        elif (profit.get("profit") or 0) <= 0:
            reason = "loss-making at this freight"
        out.append({"load": l, "papers": papers, "poster": poster, "profit": profit, "ok": reason is None,
                    "reason": reason, "urgent": bool(l.get("urgent"))})
    # urgent help first, then profit
    return sorted(out, key=lambda c: (not c["ok"], not c["urgent"], -(c["profit"].get("profit") or 0)))


async def match_empty(v: dict, notify: bool = True) -> dict:
    """The truck is empty: find, verify and book the best nearby load from another fleet (agent to agent)."""
    v = db.vehicle(v["id"])
    if v["last_lat"] is None:
        return {"error": "truck location unknown"}
    cands = candidates(v, v["last_lat"], v["last_lon"])
    checked = [{"load_id": c["load"]["id"], "route": f"{c['load']['origin']} → {c['load']['destination']}",
                "freight": c["load"]["freight"], "weight_t": c["load"]["weight_t"], "posted_by": c["poster"]["fleet"],
                "pickup_km": c["load"]["pickup_km"], "urgent": c["urgent"], "legit_score": c["poster"]["score"],
                "profit": c["profit"].get("profit"), "ok": c["ok"], "rejected_because": c["reason"]} for c in cands]
    best = next((c for c in cands if c["ok"]), None)
    db.log_agent(f"backhaul:{v['code']}", "mesh", "empty_truck_search", {"checked": checked})
    if not best:
        msg = (f"🚚 {v['reg_label']} is empty near {round(v['last_lat'], 2)}, {round(v['last_lon'], 2)}. "
               f"Checked {len(cands)} load(s) on the network — none safe & profitable. I'll keep watching.")
        if notify:
            await business._owner(v, msg)
        return {"booked": None, "checked": checked, "say": "Abhi aas-paas koi sahi load nahi mila, main dhoondhta rahunga."}
    l, poster = best["load"], db.fleet(best["load"]["fleet_id"])
    # agent-to-agent: our agent offers the truck, the poster fleet's CEO agent accepts (auto for verified carriers)
    offer = (f"{db.fleet(v['fleet_id'])['name']} offers {mask_reg(v['reg_label'])} ({v['vtype']}) for your load #{l['id']} "
             f"{l['origin']} → {l['destination']}: empty now, {l['pickup_km']} km from pickup, papers verified "
             f"({', '.join(best['papers']['valid'])}).")
    db.log_agent(f"backhaul:{v['code']}", f"ceo:{poster['code']}", "load_offer", {"text": offer})
    reply = (f"Accepted. Load #{l['id']} is yours — pickup at {l['origin']}, {l['weight_t']:g} t, freight "
             f"{inr(l['freight'])}. Loading contact will call the driver.")
    db.log_agent(f"ceo:{poster['code']}", f"backhaul:{v['code']}", "load_accept", {"text": reply})
    res = await business.accept_load(l["id"], v)
    if res.get("error"):
        return {"booked": None, "checked": checked, "error": res["error"]}
    t = db.start_trip(v["id"], l["origin"], l["destination"], l["freight"], poster["name"])
    db.ex("UPDATE trips SET origin_lat=?, origin_lon=?, dest_lat=?, dest_lon=?, weight_t=? WHERE id=?",
          (l["o_lat"], l["o_lon"], l["d_lat"], l["d_lon"], l["weight_t"], t["id"]))
    booked = {"load_id": l["id"], "route": f"{l['origin']} → {l['destination']}", "freight": l["freight"],
              "weight_t": l["weight_t"], "posted_by": poster["name"], "pickup_km": l["pickup_km"],
              "urgent": best["urgent"], "profit": best["profit"].get("profit"), "km": best["profit"].get("approx_km"),
              "papers": best["papers"]["valid"], "legit": best["poster"]["notes"], "offer": offer, "reply": reply}
    say = (f"Khaali gaadi ke liye kaam mil gaya! {poster['name'].replace(' (demo)', '')} ka load — {l['origin']} se "
           f"{l['destination']}, {l['weight_t']:g} ton, bhaada {inr(l['freight'])}. Pickup {l['pickup_km']} km pe hai. "
           f"Kaagaz sab check kar liye, party bhi verified hai. Unload ke baad seedha pickup pe chaliye.")
    if notify:
        await bus.send(to_driver(v, f"🔁 Next load booked: {l['origin']} → {l['destination']} · {inr(l['freight'])}", speak=say))
        await business._owner(v, f"🔁 Empty-truck agent booked {v['reg_label']}: load #{l['id']} {l['origin']} → "
                                 f"{l['destination']} ({poster['name']}), {l['weight_t']:g} t, {inr(l['freight'])}, est. profit "
                                 f"{inr(booked['profit'] or 0)}. Papers valid for the trip; poster: {', '.join(best['poster']['notes'][:3])}. "
                                 f"Checked {len(cands)} loads, rejected {sum(1 for c in cands if not c['ok'])}.")
    return {"booked": booked, "checked": checked, "say": say}
