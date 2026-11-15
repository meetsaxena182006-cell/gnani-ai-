"""Driver / truck efficiency from real logged data: mileage (km per litre), cost per km, driving events, tips."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db


def truck_performance(vid: int, days: int = 30) -> dict:
    v = db.vehicle(vid)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    trips = db.q("SELECT * FROM trips WHERE vehicle_id=? AND (started_at>=? OR status='active')", (vid, since))
    km = round(sum(t.get("gps_km") or 0 for t in trips))
    ex = db.q("SELECT category, amount, litres FROM expenses WHERE vehicle_id=? AND status='confirmed' AND created_at>=?",
              (vid, since))
    litres = sum(e["litres"] or 0 for e in ex if e["category"] == "diesel")
    diesel_rs = sum(e["amount"] for e in ex if e["category"] == "diesel")
    if not litres and diesel_rs:
        f = db.fleet(v["fleet_id"]) or {}
        litres = diesel_rs / (f.get("diesel_price") or 90)
    spent = sum(e["amount"] for e in ex if e["category"] != "freight")
    freight = sum(t.get("freight_amount") or 0 for t in trips)
    ev = {r["kind"]: r["n"] for r in db.q("SELECT kind, COUNT(*) n FROM watch_events WHERE vehicle_id=? AND created_at>=? "
                                         "GROUP BY kind", (vid, since))}
    expected = v.get("mileage_loaded") or 3.5
    kmpl = round(km / litres, 2) if litres and km > 50 else None
    tips = []
    if kmpl and kmpl < expected * 0.92:
        lost = round((km / kmpl - km / expected) * ((diesel_rs / litres) if litres else 90))
        tips.append(f"average {kmpl} km/l hai, honi chahiye {expected} — lagbhag ₹{lost:,} ka diesel zyada laga; tyre ki hawa, "
                    "air filter check karwaiye aur 55-65 km/h pe chalaiye")
    elif kmpl:
        tips.append(f"average {kmpl} km/l badhiya hai (umeed {expected} thi)")
    if ev.get("overspeed"):
        tips.append(f"{ev['overspeed']} baar overspeed — 65 ke upar har 10 km/h pe ~8% diesel zyada")
    if ev.get("long_stop"):
        tips.append(f"{ev['long_stop']} lambe stop — rukte waqt engine band rakhiye (idle mein ~2 litre/ghanta jalta hai)")
    if not tips:
        tips.append("diesel bharwate waqt litre bhi boliye (jaise 'diesel 120 litre 10800'), main aapka asli average naap dunga; "
                    "tab tak: tyre ki hawa sahi rakhiye, 55-65 km/h, rukte waqt engine band")
    score = 100 - 6 * ev.get("overspeed", 0) - 3 * ev.get("long_stop", 0) - 4 * ev.get("route_deviation", 0)
    if kmpl and kmpl < expected:
        score -= round(30 * (1 - kmpl / expected))
    score = max(30, min(100, score))
    say = (f"Pichhle {days} din: {km} km chale, diesel {round(litres)} litre (₹{round(diesel_rs):,}), "
           + (f"average {kmpl} km/l. " if kmpl else "average nikaalne ke liye diesel ke litre chahiye. ")
           + (f"Kharcha ₹{round(spent / km, 1)}/km. " if km > 50 and spent else "")
           + f"Driving score {score}/100. " + "Sujhav: " + "; ".join(tips) + ".")
    return {"say": say, "days": days, "km": km, "diesel_litres": round(litres), "diesel_rs": round(diesel_rs), "kmpl": kmpl,
            "expected_kmpl": expected, "cost_per_km": round(spent / km, 1) if km > 50 and spent else None,
            "freight": round(freight), "events": ev, "driving_score": score, "tips": tips}
