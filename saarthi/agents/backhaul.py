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
import os
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

# ---------------- demo load market (synthetic, so the live link always has work to match) ----------------
CITIES = [("Udaipur", 24.585, 73.712), ("Ahmedabad", 23.0225, 72.5714), ("Jaipur", 26.912, 75.787),
          ("Indore", 22.7196, 75.8577), ("Surat", 21.1702, 72.8311), ("Mumbai (Bhiwandi)", 19.2813, 73.0483),
          ("Delhi (Kundli)", 28.8747, 77.1068), ("Hyderabad", 17.385, 78.4867), ("Pune", 18.5204, 73.8567),
          ("Kota", 25.2138, 75.8648), ("Vadodara", 22.3072, 73.1812), ("Bhopal", 23.2599, 77.4126)]
GOODS = ["marble slabs", "granite blocks", "cement bags", "tiles", "cotton bales", "steel coils", "FMCG cartons"]


def _quick_fleet() -> dict:
    q = db.one("SELECT * FROM fleets WHERE code='QUICK'")
    if not q:   # a poster with no KYC and unpaid dues: the agent must refuse it even though it pays more
        db.ex("INSERT INTO fleets(code,name,diesel_price,verified,rating,overdue_payments) "
              "VALUES('QUICK','Quick Cargo Movers (demo, unverified)',92,0,2.1,2)")
        q = db.one("SELECT * FROM fleets WHERE code='QUICK'")
    return q


def ensure_market(v: dict) -> int:
    """DEMO only (DEMO_MARKET=0 turns it off): if no other fleet has posted work near this empty truck, post three
    synthetic loads near it - one safe & urgent, one from an untrustworthy poster, one too heavy - so anyone
    testing the live link sees the agent check papers and legitimacy for real. Returns loads added."""
    import random
    if os.getenv("DEMO_MARKET", "1") != "1" or v.get("last_lat") is None:
        return 0
    lat, lon = v["last_lat"], v["last_lon"]
    prof = costing.PROFILES.get(v["vtype"])
    cap = prof.payload_tonnes if prof else 10
    others = [f for f in db.q("SELECT * FROM fleets WHERE verified=1 AND id<>?", (v["fleet_id"],))]
    if not others:
        return 0
    ids = {f["id"] for f in others}
    fits = [l for l in business.loads_near(lat, lon, exclude_fleet=v["fleet_id"], radius_km=SEARCH_KM)
            if l["fleet_id"] in ids and (l.get("weight_t") or 0) <= cap and l.get("vtype") in (None, "", v["vtype"])]
    if fits:      # real (or earlier) work this truck could take is already posted nearby
        return 0
    near = min(CITIES, key=lambda c: geo.haversine_km(lat, lon, c[1], c[2]))
    far = sorted((c for c in CITIES if 250 <= geo.haversine_km(lat, lon, c[1], c[2]) <= 1300),
                 key=lambda c: geo.haversine_km(lat, lon, c[1], c[2]))[:4] or [c for c in CITIES if c != near][:3]
    rnd = random.Random(f"{v['id']}-{date.today()}")
    ins = ("INSERT INTO loads(fleet_id,origin,destination,o_lat,o_lon,d_lat,d_lon,weight_t,vtype,freight,status,urgent,"
           "need,created_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'open', ?, ?, ?)")
    added = 0
    for i, (kind, fleet) in enumerate([("safe", rnd.choice(others)), ("risky", _quick_fleet()), ("heavy", rnd.choice(others))]):
        dest = far[i % len(far)]
        o_lat, o_lon = lat + rnd.uniform(-0.15, 0.15), lon + rnd.uniform(-0.15, 0.15)
        km = geo.haversine_km(o_lat, o_lon, dest[1], dest[2]) * 1.25
        rate = {"safe": 78, "risky": 105, "heavy": 85}[kind]
        weight = {"safe": round(cap * 0.85), "risky": round(cap * 0.8), "heavy": round(cap + 6)}[kind]
        freight = int(round(km * rate / 1000) * 1000)
        need = {"safe": f"URGENT: our own truck is stuck, {rnd.choice(GOODS)} must leave today (demo market)",
                "risky": "cash on delivery, pay later (demo market)",
                "heavy": f"{weight} t {rnd.choice(GOODS)} (demo market)"}[kind]
        db.ex(ins, (fleet["id"], f"Near {near[0]}", dest[0], round(o_lat, 4), round(o_lon, 4), dest[1], dest[2], weight,
                    v["vtype"], freight, 1 if kind == "safe" else 0, need, db.now()))
        added += 1
    log.info("demo market: posted %d synthetic loads near %s for %s", added, near[0], v["code"])
    return added


async def match_empty(v: dict, notify: bool = True) -> dict:
    """The truck is empty: find, verify and book the best nearby load from another fleet (agent to agent)."""
    v = db.vehicle(v["id"])
    if v["last_lat"] is None:
        return {"error": "truck location unknown"}
    ensure_market(v)
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
        why = sorted({c["reason"] for c in cands if c["reason"]})
        say = "Abhi aas-paas koi sahi load nahi mila, main dhoondhta rahunga."
        docs = [p for c in cands for p in c["papers"]["problems"] if any(d in p for d in REQUIRED_DOCS)]
        if docs and any(c["poster"]["pass"] for c in cands):
            say = ("Load mile, par gaadi ke kaagaz is trip tak valid nahi hain — " + "; ".join(sorted(set(docs)))[:200] +
                   ". Pehle renew karwaiye, phir main book kar dunga.")
        elif cands:
            say = (f"{len(cands)} load dekhe, koi safe nahi tha — " + "; ".join(why)[:220] + ". Main dhoondhta rahunga.")
        return {"booked": None, "checked": checked, "rejected_reasons": why, "say": say}
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
