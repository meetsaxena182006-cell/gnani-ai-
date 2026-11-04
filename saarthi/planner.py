"""Load & route planning: the most profitable, safest plan for each trip and for the whole fleet.

TRIP PLANNER (one truck, one load)
  For every route option (OSRM alternatives / Google traffic-aware) x every departure time in the next 24 h, it
  simulates the drive in 5 km steps:
    - speed: truck-adjusted route speed, slowed by forecast weather at that place and hour (fog 0.6x, heavy rain 0.75x,
      likely rain / strong wind 0.9x) and by Google live traffic if available for an immediate departure;
    - driver rules (Motor Transport Workers Act 1961, via MV Act s.91): a 30-min rest after at most 5 h of work,
      8 h of driving per duty day (16 h with a relief driver), 10 h spread-over, then an 8 h sleep;
    - health: a 15-min stretch/chai break every 2 h of continuous driving; night (00:00-05:00) driving is penalised;
    - fuel: refuel (combined with a break) before the tank falls below 15%;
    - city no-entry windows for trucks (data/truck_restrictions.json, owner-maintained) -> waiting time.
  Each plan is scored in rupees: freight - diesel - tolls (NHAI plazas on that route) - maintenance - time cost
  (driver + fixed cost per hour) - risk penalties (night, fog, heavy rain) - lateness vs the unloading window.
  The best plan, the fastest and the "leave now" plan are returned with the reasons, the stop schedule (named
  pumps/dhabas near each stop from OpenStreetMap), and the cost/profit.

FLEET LOAD PLANNER (all trucks x all loads)
  For each truck (where/when it becomes free) and each open load (own orders + Saarthi-network loads): empty km to
  pickup, capacity check, profit after deadhead. Optimal assignment (Hungarian algorithm) maximises total profit;
  then part-loads are consolidated into trucks with spare capacity when pickup and drop are on the way.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import costing, db, geo, tolls
from .agents.common import IST, inr

STEP_KM = 5.0
HEALTH_BREAK_H = 2.0          # 15-min break after 2 h continuous driving (health)
HEALTH_BREAK_MIN = 15
LEGAL_REST_AFTER_H = 5.0      # MTW Act: rest of >= 30 min after at most 5 h of work
LEGAL_REST_MIN = 30
MEAL_REST_MIN = 45            # the legal rest is taken as a meal break at a dhaba
DAILY_DRIVE_H = 8.0           # MTW Act: 8 h work/day
RELIEF_DRIVE_H = 16.0         # two drivers taking turns
SPREAD_H = 10.0               # spread-over incl. breaks (MTW Act s.16; verify state rules)
SLEEP_H = 8.0
REFUEL_MIN = 20
RESERVE_FRAC = 0.15
NIGHT_HOURS = (0, 1, 2, 3, 4)
PENALTY = {"night_h": 400, "fog_h": 500, "heavy_rain_h": 300, "late_h": 2000, "late_fixed": 10000}
RESTRICTIONS = Path(__file__).resolve().parent.parent / "data" / "truck_restrictions.json"


# ------------------------------ helpers ------------------------------
def _cum_km(coords: list[tuple[float, float]]) -> list[float]:
    out = [0.0]
    for a, b in zip(coords, coords[1:]):
        out.append(out[-1] + geo.haversine_km(a[0], a[1], b[0], b[1]))
    return out


def _point_at(coords, cum, km) -> tuple[float, float]:
    if km <= 0:
        return coords[0]
    if km >= cum[-1]:
        return coords[-1]
    lo, hi = 0, len(cum) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if cum[mid] <= km else (lo, mid)
    seg = cum[hi] - cum[lo] or 1e-9
    f = (km - cum[lo]) / seg
    a, b = coords[lo], coords[hi]
    return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))


def _ist(t: datetime) -> str:
    return t.astimezone(IST).strftime("%a %d %b %H:%M")


def load_restrictions() -> list[dict]:
    if not RESTRICTIONS.exists():
        return []
    return [r for r in json.loads(RESTRICTIONS.read_text(encoding="utf-8")).get("cities", []) if r.get("enabled")]


def _no_entry_wait(lat: float, lon: float, t: datetime, rules: list[dict]) -> tuple[float, str | None]:
    """Hours to wait if arriving inside a city's truck no-entry window."""
    local = t.astimezone(IST)
    for r in rules:
        if geo.haversine_km(lat, lon, r["lat"], r["lon"]) > r.get("radius_km", 15):
            continue
        for start, end in r.get("no_entry", []):
            s = local.replace(hour=int(start[:2]), minute=int(start[3:]), second=0, microsecond=0)
            e = local.replace(hour=int(end[:2]), minute=int(end[3:]), second=0, microsecond=0)
            if e <= s:
                e += timedelta(days=1)
                if local < s and local.hour < int(end[:2]):
                    s -= timedelta(days=1); e -= timedelta(days=1)
            if s <= local < e:
                return (e - local).total_seconds() / 3600, r["city"]
    return 0.0, None


class Weather:
    """Hourly forecasts at sample points along a route; answers 'what is it like at km X at time T'."""

    def __init__(self, samples: list[tuple[float, dict]]):
        self.samples = samples  # [(km_along_route, open-meteo json)]

    def at(self, km: float, t: datetime) -> dict:
        if not self.samples:
            return {}
        km0, js = min(self.samples, key=lambda s: abs(s[0] - km))
        h = js.get("hourly", {})
        times = h.get("time") or []
        key = t.astimezone(IST).strftime("%Y-%m-%dT%H:00")
        if key not in times:
            return {}
        i = times.index(key)
        g = lambda k: (h.get(k) or [None] * len(times))[i]
        return {"code": g("weather_code"), "vis": g("visibility"), "rain": g("precipitation"),
                "prob": g("precipitation_probability"), "temp": g("temperature_2m"), "wind": g("wind_speed_10m")}

    @staticmethod
    def factor(w: dict) -> tuple[float, str | None]:
        code, vis = w.get("code"), w.get("vis")
        if code in (45, 48) or (vis is not None and vis < 1000):
            return 0.6, "fog"
        if code in (65, 82, 95, 96, 99) or (w.get("rain") or 0) > 7:
            return 0.75, "heavy_rain"
        if (w.get("prob") or 0) >= 70 or (w.get("wind") or 0) >= 45:
            return 0.9, "rain_or_wind"
        return 1.0, None


@dataclass
class TripRequest:
    origin: tuple[float, float]
    destination: tuple[float, float]
    vtype: str = "truck_10w"
    origin_name: str = "origin"
    destination_name: str = "destination"
    load_tonnes: float | None = None
    freight: float | None = None
    depart_after: datetime | None = None
    latest_arrival: datetime | None = None
    fuel_now_l: float | None = None
    relief_driver: bool = False
    diesel_price: float = 90.0
    fixed_per_day: float | None = None
    search_hours: int = 24


@dataclass
class Sim:
    route: int
    depart: datetime
    arrive: datetime
    driving_h: float
    stops: list[dict] = field(default_factory=list)
    night_h: float = 0.0
    hazard_h: dict = field(default_factory=dict)
    no_entry_wait_h: float = 0.0
    refuels: int = 0


def simulate(req: TripRequest, route_idx: int, coords, distance_km: float, base_hours: float, depart: datetime,
             weather: Weather, rules: list[dict], traffic_delay_h: float = 0.0) -> Sim:
    p = costing.PROFILES.get(req.vtype, costing.PROFILES["truck_10w"])
    cum = _cum_km(coords)
    scale = distance_km / cum[-1] if cum[-1] else 1.0
    base_speed = min(55.0, distance_km / max(base_hours + traffic_delay_h, 0.1))
    daily_limit = RELIEF_DRIVE_H if req.relief_driver else DAILY_DRIVE_H
    tank = p.tank_l
    fuel = req.fuel_now_l if req.fuel_now_l is not None else tank * 0.9
    t, km = depart, 0.0
    duty_start, duty_drive, since_break, since_rest = t, 0.0, 0.0, 0.0
    sim = Sim(route_idx, depart, depart, 0.0)

    def stop(kind, minutes, note=""):
        nonlocal t
        lat, lon = _point_at(coords, cum, km / scale)
        sim.stops.append({"kind": kind, "at_km": round(km), "time": t, "minutes": int(minutes), "lat": lat, "lon": lon, "note": note})
        t = t + timedelta(minutes=minutes)

    while km < distance_km - 1e-6:
        # --- rules before driving the next stretch ---
        need_l = (min(STEP_KM * 20, distance_km - km)) / p.kmpl_loaded   # next ~100 km
        low_fuel = fuel - need_l < tank * RESERVE_FRAC and distance_km - km > STEP_KM
        if duty_drive >= daily_limit - 1e-9 or (not req.relief_driver and (t - duty_start).total_seconds() / 3600 >= SPREAD_H):
            stop("sleep", SLEEP_H * 60, "daily driving limit reached — night halt")
            duty_start, duty_drive, since_break, since_rest = t, 0.0, 0.0, 0.0
            continue
        if since_rest >= LEGAL_REST_AFTER_H - 0.5:  # take the legal rest a little before the 5 h mark
            mins = max(LEGAL_REST_MIN, MEAL_REST_MIN)
            if low_fuel:
                fuel = tank; sim.refuels += 1
            stop("meal_rest" + ("+refuel" if low_fuel else ""), mins, "legal 30-min rest taken as a meal break")
            since_break = since_rest = 0.0
            continue
        if since_break >= HEALTH_BREAK_H or low_fuel:
            if low_fuel:
                fuel = tank; sim.refuels += 1
            stop("refuel" if low_fuel else "break", max(HEALTH_BREAK_MIN, REFUEL_MIN if low_fuel else 0),
                 "diesel + stretch break" if low_fuel else "chai / stretch / walk around the truck")
            since_break = 0.0
            continue
        # --- drive one step ---
        step = min(STEP_KM, distance_km - km)
        w = weather.at(km, t)
        f, hz = Weather.factor(w)
        local_hour = t.astimezone(IST).hour
        speed = base_speed * f * (0.95 if local_hour in NIGHT_HOURS else 1.0)
        dt = step / speed
        if local_hour in NIGHT_HOURS:
            sim.night_h += dt
        if hz:
            sim.hazard_h[hz] = sim.hazard_h.get(hz, 0.0) + dt
        t += timedelta(hours=dt)
        km += step
        fuel -= step / p.kmpl_loaded
        duty_drive += dt; since_break += dt; since_rest += dt; sim.driving_h += dt
    wait, city = _no_entry_wait(coords[-1][0], coords[-1][1], t, rules)
    if wait:
        sim.no_entry_wait_h = wait
        stop("no_entry_wait", wait * 60, f"truck no-entry in {city}")
    sim.arrive = t
    return sim


def score(req: TripRequest, sim: Sim, distance_km: float, toll_total: float | None) -> dict:
    total_h = (sim.arrive - sim.depart).total_seconds() / 3600
    days = max(1, math.ceil(total_h / 24))
    est = costing.estimate(costing.CostInputs(
        distance_km=distance_km, vtype=req.vtype, diesel_price=req.diesel_price, load_tonnes=req.load_tonnes,
        actual_tolls=toll_total if toll_total else None, offered_freight=req.freight, fixed_per_day=req.fixed_per_day,
        trip_days=days, helper_per_day=0))
    lines = est["cost_lines"]
    per_day_time = (est["assumptions"]["driver_salary_per_day"] + est["assumptions"]["driver_bata_per_day"]
                    + est["assumptions"]["misc_per_day"] + est["assumptions"]["fixed_per_day"]) * (2 if req.relief_driver else 1)
    variable = lines["diesel"] + lines["tolls"] + lines["maintenance_tyres"]
    time_cost = per_day_time * total_h / 24
    risk = (PENALTY["night_h"] * sim.night_h + PENALTY["fog_h"] * sim.hazard_h.get("fog", 0)
            + PENALTY["heavy_rain_h"] * sim.hazard_h.get("heavy_rain", 0))
    late_h = 0.0
    if req.latest_arrival and sim.arrive > req.latest_arrival:
        late_h = (sim.arrive - req.latest_arrival).total_seconds() / 3600
        risk += PENALTY["late_fixed"] + PENALTY["late_h"] * late_h
    freight = req.freight or 0
    value = freight - variable - time_cost - risk
    return {"score": value, "total_h": total_h, "variable_cost": variable, "time_cost": time_cost, "risk_penalty": risk,
            "late_h": late_h, "estimate": est, "days": days}


async def plan_trip(req: TripRequest, with_places: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    start = (req.depart_after or now).astimezone(timezone.utc)
    start = start.replace(second=0, microsecond=0) + timedelta(minutes=(30 - start.minute % 30) % 30)
    r = await geo.route(req.origin[0], req.origin[1], req.destination[0], req.destination[1])
    options = []
    for i, rt in enumerate(r.get("routes", [])[:3]):
        coords = rt.get("_coords") or [req.origin, req.destination]
        base_h = rt.get("truck_duration_h") or (rt.get("duration_h", rt["distance_km"] / 45) * geo.TRUCK_FACTOR)
        tl = tolls.on_route(coords, req.vtype) if len(coords) > 2 else {"plazas": [], "expected_total": 0}
        options.append({"i": i, "coords": coords, "km": rt["distance_km"], "base_h": base_h, "tolls": tl,
                        "traffic_delay_h": (rt.get("traffic_delay_min") or 0) / 60, "via": rt.get("via", "")})
    if not options:
        return {"error": "no route found"}
    # weather samples every ~100 km on the main route (one forecast call each, reused for all departures)
    main = options[0]
    cum = _cum_km(main["coords"])
    n = max(2, min(6, int(main["km"] // 100) + 2))
    samples = []
    for k in range(n):
        km_along = cum[-1] * k / (n - 1)
        lat, lon = _point_at(main["coords"], cum, km_along)
        try:
            samples.append((km_along * main["km"] / (cum[-1] or 1), await geo.forecast_hourly(lat, lon)))
        except Exception:
            pass
    weather = Weather(samples)
    rules = load_restrictions()
    results = []
    for opt in options:
        for h in range(0, req.search_hours + 1):
            dep = start + timedelta(hours=h)
            live_traffic = opt["traffic_delay_h"] if h == 0 else 0.0  # live traffic only valid for leaving now
            sim = simulate(req, opt["i"], opt["coords"], opt["km"], opt["base_h"], dep, weather, rules, live_traffic)
            sc = score(req, sim, opt["km"], opt["tolls"]["expected_total"])
            results.append((sc["score"], opt, sim, sc))
    best = max(results, key=lambda x: x[0])
    fastest = min(results, key=lambda x: (x[3]["total_h"], -x[0]))
    leave_now = max((x for x in results if x[2].depart == start), key=lambda x: x[0])
    plan = _describe(req, best, weather_used=bool(samples), traffic=r.get("traffic"))
    if with_places:
        await _name_stops(plan)
    plan["alternatives"] = {"leave_now": _brief(leave_now), "fastest": _brief(fastest)}
    plan["why"] = _reasons(best, leave_now, fastest)
    return plan


def _brief(x) -> dict:
    _, opt, sim, sc = x
    return {"route": opt["i"] + 1, "depart": _ist(sim.depart), "arrive": _ist(sim.arrive), "total_h": round(sc["total_h"], 1),
            "night_driving_h": round(sim.night_h, 1), "fog_h": round(sim.hazard_h.get("fog", 0), 1),
            "profit_estimate": round((sc["estimate"].get("profit_at_offer") or -sc["estimate"]["total_cost"])),
            "score": round(x[0])}


def _describe(req: TripRequest, x, weather_used: bool, traffic) -> dict:
    _, opt, sim, sc = x
    est = sc["estimate"]
    return {
        "route": {"option": opt["i"] + 1, "km": opt["km"], "via": opt["via"], "toll_plazas": opt["tolls"]["plazas"],
                  "tolls_estimate": opt["tolls"]["expected_total"]},
        "depart": _ist(sim.depart), "arrive": _ist(sim.arrive), "depart_iso": sim.depart.isoformat(),
        "arrive_iso": sim.arrive.isoformat(), "total_h": round(sc["total_h"], 1), "driving_h": round(sim.driving_h, 1),
        "stops": [{**s, "time": _ist(s["time"])} for s in sim.stops],
        "night_driving_h": round(sim.night_h, 1),
        "weather_risk_h": {k: round(v, 1) for k, v in sim.hazard_h.items()},
        "no_entry_wait_h": round(sim.no_entry_wait_h, 1), "late_h": round(sc["late_h"], 1),
        "relief_driver": req.relief_driver, "refuel_stops": sim.refuels,
        "cost": est["cost_lines"], "total_cost": est["total_cost"], "freight": req.freight,
        "profit": est.get("profit_at_offer"), "margin_pct": est.get("margin_at_offer_pct"),
        "data": {"weather": "Open-Meteo hourly forecast along the route" if weather_used else "not available",
                 "traffic": traffic or "n/a", "tolls": "NHAI plazas on route (2019-20 rates, a minimum)",
                 "driver_rules": "MTW Act 1961: 30-min rest after <=5 h, 8 h/day driving, 10 h spread-over"},
        "_route_coords": opt["coords"],
    }


def _reasons(best, now, fastest) -> list[str]:
    _, bo, bs, bsc = best
    _, no, ns, nsc = now
    out = []
    if bs.depart != ns.depart:
        gain = []
        if ns.night_h - bs.night_h >= 0.5:
            gain.append(f"{ns.night_h - bs.night_h:.1f} h less night driving")
        if ns.hazard_h.get("fog", 0) - bs.hazard_h.get("fog", 0) >= 0.5:
            gain.append(f"{ns.hazard_h.get('fog', 0) - bs.hazard_h.get('fog', 0):.1f} h less fog")
        if ns.no_entry_wait_h - bs.no_entry_wait_h >= 0.5:
            gain.append(f"{ns.no_entry_wait_h - bs.no_entry_wait_h:.1f} h less no-entry waiting")
        if nsc["late_h"] > bsc["late_h"]:
            gain.append("reaches within the unloading window")
        out.append(f"Leave at {_ist(bs.depart)} instead of now: " + (", ".join(gain) or "better overall cost/risk")
                   + f" (score {inr(best[0])} vs {inr(now[0])}).")
    if bo["i"] != 0:
        out.append(f"Route option {bo['i'] + 1} chosen ({bo['km']} km, tolls ≈ {inr(bo['tolls']['expected_total'])}).")
    if fastest[2].depart != bs.depart or fastest[1]["i"] != bo["i"]:
        out.append(f"Fastest plan would take {fastest[3]['total_h']:.1f} h vs {bsc['total_h']:.1f} h, but with more risk/cost.")
    return out or ["Leaving now on the main route is already the best plan."]


async def _name_stops(plan: dict) -> None:
    pts = [(s["lat"], s["lon"]) for s in plan["stops"] if s["kind"] != "no_entry_wait"][:10]
    try:
        near = await geo.overpass_along(pts)
    except Exception:
        return
    i = 0
    for s in plan["stops"]:
        if s["kind"] == "no_entry_wait" or i >= len(near):
            continue
        if near[i]:
            s["place"] = f"{near[i][0]['name']} ({near[i][0]['distance_km']} km off route)"
        i += 1


def speakable_plan(plan: dict) -> str:
    stops = [s for s in plan["stops"] if s["kind"] in ("break", "meal_rest", "meal_rest+refuel", "refuel", "sleep")]
    first = stops[0] if stops else None
    msg = f"Plan taiyaar: {plan['depart']} nikliye, {plan['arrive']} tak pahunchenge, kul {plan['total_h']} ghante, {len(stops)} stop."
    if first:
        msg += f" Pehla {('chai break' if first['kind'] == 'break' else 'khana aur aaraam' if 'meal' in first['kind'] else 'stop')} {first['at_km']} kilometre pe"
        msg += f", {first['place'].split(' (')[0]} ke paas." if first.get("place") else "."
    if plan["weather_risk_h"].get("fog"):
        msg += " Raste mein kohra milega, dhyan se."
    return msg


def owner_summary(plan: dict, title: str) -> str:
    lines = [f"🗺️ Trip plan — {title}",
             f"Route {plan['route']['option']}: {plan['route']['km']} km · tolls ≈ {inr(plan['route']['tolls_estimate'])}",
             f"Depart {plan['depart']} → arrive {plan['arrive']} ({plan['total_h']} h, driving {plan['driving_h']} h)"]
    for s in plan["stops"]:
        lines.append(f"  • {s['time']} km {s['at_km']}: {s['kind'].replace('_', ' ')} {s['minutes']} min"
                     + (f" — {s['place']}" if s.get("place") else ""))
    risks = [f"night {plan['night_driving_h']} h"] if plan["night_driving_h"] else []
    risks += [f"{k.replace('_', ' ')} {v} h" for k, v in plan["weather_risk_h"].items()]
    if risks:
        lines.append("Risks: " + ", ".join(risks))
    if plan.get("profit") is not None:
        leg = " (remaining leg only; costs already spent are in the trip P&L)" if plan.get("start_gps_km") else ""
        lines.append(f"Cost {inr(plan['total_cost'])} · freight {inr(plan['freight'])} · profit {inr(plan['profit'])} "
                     f"({plan['margin_pct']}%){leg}")
    lines += ["Why: " + r for r in plan.get("why", [])]
    return "\n".join(lines)


# ============================ FLEET LOAD PLANNER ============================
def hungarian_max(profit: list[list[float]]) -> list[tuple[int, int]]:
    """Optimal assignment maximising total profit (rows=trucks, cols=loads). Classic O(n^3) Hungarian."""
    n_r, n_c = len(profit), len(profit[0]) if profit else 0
    n = n_r + n_c  # pad so every truck can also "stay idle" (profit 0) instead of taking a loss-making load
    if n == 0:
        return []
    big = max((abs(v) for row in profit for v in row), default=0) + 1
    cost = [[(big - profit[i][j]) if i < n_r and j < n_c else big for j in range(n)] for i in range(n)]
    INF = float("inf")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (n + 1), [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [INF] * (n + 1), [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta; v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]; p[j0] = p[j1]; j0 = j1
            if j0 == 0:
                break
    return [(p[j] - 1, j - 1) for j in range(1, n + 1) if p[j] and p[j] - 1 < n_r and j - 1 < n_c]


def truck_availability(v: dict) -> dict | None:
    t = db.active_trip(v["id"])
    now = datetime.now(timezone.utc)
    if t and t.get("dest_lat") is not None and v.get("last_lat") is not None:
        remaining = geo.haversine_km(v["last_lat"], v["last_lon"], t["dest_lat"], t["dest_lon"]) * 1.25
        return {"lat": t["dest_lat"], "lon": t["dest_lon"], "free_at": now + timedelta(hours=remaining / 40 + 3),
                "where": t["destination"]}
    if v.get("last_lat") is not None:
        return {"lat": v["last_lat"], "lon": v["last_lon"], "free_at": now, "where": "current location"}
    return None


def pair_value(v: dict, avail: dict, load: dict, diesel_price: float) -> dict | None:
    p = costing.PROFILES.get(v["vtype"], costing.PROFILES["truck_10w"])
    if load.get("weight_t") and load["weight_t"] > p.payload_tonnes:
        return None
    deadhead = geo.haversine_km(avail["lat"], avail["lon"], load["o_lat"], load["o_lon"]) * 1.25
    loaded = geo.haversine_km(load["o_lat"], load["o_lon"], load["d_lat"], load["d_lon"]) * 1.25
    est = costing.estimate(costing.CostInputs(distance_km=max(loaded, 1), vtype=v["vtype"], diesel_price=diesel_price,
                                              load_tonnes=load.get("weight_t"), offered_freight=load["freight"]))
    deadhead_cost = deadhead / p.kmpl_empty * diesel_price + deadhead * p.maintenance_per_km \
        + (deadhead / 40) / 24 * (1200 + p.fixed_per_day)
    profit = est["profit_at_offer"] - deadhead_cost
    util = 100 * loaded / (loaded + deadhead) if loaded + deadhead else 0
    cap = 100 * (load.get("weight_t") or 0) / p.payload_tonnes if load.get("weight_t") else None
    return {"profit": round(profit), "deadhead_km": round(deadhead), "loaded_km": round(loaded),
            "km_utilisation_pct": round(util), "capacity_pct": round(cap) if cap is not None else None,
            "pickup_eta": _ist(avail["free_at"] + timedelta(hours=deadhead / 40))}


def plan_fleet(fleet_id: int) -> dict:
    f = db.fleet(fleet_id)
    trucks = []
    for v in db.q("SELECT * FROM vehicles WHERE fleet_id=?", (fleet_id,)):
        a = truck_availability(v)
        if a:
            trucks.append((v, a))
    loads = db.q("SELECT l.*, fl.name fleet_name FROM loads l JOIN fleets fl ON fl.id=l.fleet_id WHERE l.status='open' "
                 "AND (l.fleet_id=? OR fl.mesh_opt_in=1)", (fleet_id,))
    if not trucks or not loads:
        return {"assignments": [], "note": "no available trucks with location, or no open loads"}
    vals = [[pair_value(v, a, l, f["diesel_price"]) for l in loads] for v, a in trucks]
    matrix = [[(x["profit"] if x and x["profit"] > 0 else -1e9) for x in row] for row in vals]
    pairs = hungarian_max(matrix)
    assignments, used_loads = [], set()
    for ti, li in pairs:
        x = vals[ti][li]
        if not x or x["profit"] <= 0:
            continue
        v, a = trucks[ti]
        l = loads[li]
        used_loads.add(l["id"])
        assignments.append({"vehicle": v["code"], "reg": v["reg_label"], "free_at": a["where"], "load_id": l["id"],
                            "load": f"{l['origin']} → {l['destination']}", "freight": l["freight"],
                            "posted_by": l["fleet_name"], **x})
    # consolidation: add a part-load to a truck with spare capacity if pickup and drop are on the way
    for asg in assignments:
        v = db.vehicle_by_code(asg["vehicle"])
        p = costing.PROFILES.get(v["vtype"], costing.PROFILES["truck_10w"])
        main = next(l for l in loads if l["id"] == asg["load_id"])
        spare = p.payload_tonnes - (main.get("weight_t") or p.payload_tonnes)
        for l in loads:
            if l["id"] in used_loads or not l.get("weight_t") or l["weight_t"] > spare:
                continue
            o_det = geo.haversine_km(main["o_lat"], main["o_lon"], l["o_lat"], l["o_lon"])
            d_det = geo.haversine_km(main["d_lat"], main["d_lon"], l["d_lat"], l["d_lon"])
            if o_det <= 30 and d_det <= 50:
                extra_km = (o_det + d_det) * 1.25
                extra_cost = extra_km / p.kmpl_loaded * f["diesel_price"] + extra_km * p.maintenance_per_km + 600  # handling
                gain = l["freight"] - extra_cost
                if gain > 0:
                    used_loads.add(l["id"]); spare -= l["weight_t"]
                    asg.setdefault("add_on_loads", []).append({"load_id": l["id"], "load": f"{l['origin']} → {l['destination']}",
                                                               "weight_t": l["weight_t"], "extra_profit": round(gain)})
                    asg["profit"] += round(gain)
                    asg["capacity_pct"] = round(100 * (p.payload_tonnes - spare) / p.payload_tonnes)
    idle = [v["code"] for v, _ in trucks if v["code"] not in {a["vehicle"] for a in assignments}]
    unassigned = [{"load_id": l["id"], "load": f"{l['origin']} → {l['destination']}"} for l in loads if l["id"] not in used_loads]
    tot_loaded = sum(a["loaded_km"] for a in assignments)
    tot_dead = sum(a["deadhead_km"] for a in assignments)
    return {"assignments": assignments, "total_profit": sum(a["profit"] for a in assignments),
            "fleet_km_utilisation_pct": round(100 * tot_loaded / (tot_loaded + tot_dead)) if tot_loaded + tot_dead else None,
            "idle_trucks": idle, "unassigned_loads": unassigned,
            "method": "optimal assignment (Hungarian) on profit after empty km; road km ≈ 1.25 × straight line"}


# ============================ agent entry points ============================
def _parse_when(s: str | None) -> datetime | None:
    if not s or s.lower() in ("now", "abhi"):
        return None
    try:
        t = datetime.fromisoformat(s)
        return t.replace(tzinfo=IST) if t.tzinfo is None else t
    except ValueError:
        return None


async def plan_for_vehicle(v: dict, depart_after: str | None = None, latest_arrival: str | None = None,
                           relief_driver: bool = False, notify: bool = True) -> dict:
    """Plan the truck's active trip from where it is now; save it; update the CEO (owner) and the driver."""
    from .bus import bus, to_driver
    t = db.active_trip(v["id"])
    if not t:
        return {"error": "no active trip"}
    if v.get("last_lat") is None:
        return {"error": "truck location unknown"}
    if t.get("dest_lat") is None:
        d = await geo.geocode(t["destination"])
        if not d:
            return {"error": "destination not found on map"}
        db.ex("UPDATE trips SET dest_lat=?, dest_lon=? WHERE id=?", (d["lat"], d["lon"], t["id"]))
        t = db.active_trip(v["id"])
    f = db.fleet(v["fleet_id"])
    req = TripRequest(origin=(v["last_lat"], v["last_lon"]), destination=(t["dest_lat"], t["dest_lon"]), vtype=v["vtype"],
                      origin_name=t["origin"], destination_name=t["destination"], load_tonnes=t.get("weight_t"),
                      freight=t["freight_amount"], depart_after=_parse_when(depart_after),
                      latest_arrival=_parse_when(latest_arrival), relief_driver=relief_driver, diesel_price=f["diesel_price"])
    plan = await plan_trip(req)
    if "error" in plan:
        return plan
    plan["start_gps_km"] = t.get("gps_km") or 0
    plan.pop("_route_coords", None)
    save_plan(t["id"], plan)
    db.log_agent(f"driver:{v['code']}", f"ceo:{f['code']}", "trip_planned",
                 {"depart": plan["depart"], "arrive": plan["arrive"], "stops": len(plan["stops"]), "profit": plan.get("profit")})
    if notify:
        from .agents import ceo
        o = ceo.owner_out(f, owner_summary(plan, f"{v['reg_label']} {t['origin']} → {t['destination']}"))
        if o:
            await bus.send(o)
        await bus.send(to_driver(v, "🗺️ " + speakable_plan(plan), speak=speakable_plan(plan)))
    return plan


async def plan_request(fleet_id: int, origin: str, destination: str, vtype: str = "truck_10w",
                       load_tonnes: float | None = None, freight: float | None = None, depart_after: str | None = None,
                       latest_arrival: str | None = None, relief_driver: bool = False) -> dict:
    o, d = await geo.geocode(origin), await geo.geocode(destination)
    if not o or not d:
        return {"error": "place not found on map"}
    f = db.fleet(fleet_id)
    plan = await plan_trip(TripRequest(origin=(o["lat"], o["lon"]), destination=(d["lat"], d["lon"]), vtype=vtype,
                                       origin_name=origin, destination_name=destination, load_tonnes=load_tonnes,
                                       freight=freight, depart_after=_parse_when(depart_after),
                                       latest_arrival=_parse_when(latest_arrival), relief_driver=relief_driver,
                                       diesel_price=f["diesel_price"]))
    plan.pop("_route_coords", None)
    if "error" not in plan:
        plan["summary"] = owner_summary(plan, f"{origin} → {destination}")
    return plan


# ============================ persistence & monitoring ============================
def save_plan(trip_id: int, plan: dict) -> None:
    db.ex("INSERT INTO trip_plans(trip_id, plan, created_at) VALUES(?,?,?)",
          (trip_id, json.dumps({k: v for k, v in plan.items()}, default=str, ensure_ascii=False), db.now()))


def latest_plan(trip_id: int) -> dict | None:
    row = db.one("SELECT plan FROM trip_plans WHERE trip_id=? ORDER BY id DESC LIMIT 1", (trip_id,))
    return json.loads(row["plan"]) if row else None


async def monitor() -> None:
    """Every minute: upcoming planned stops -> companion reminder; behind plan -> CEO/owner update with new ETA.
    The driver is never pushed to drive faster to recover time."""
    from .bus import bus, to_driver
    from .agents import ceo
    from .business import _once
    now = datetime.now(timezone.utc)
    for t in db.q("SELECT * FROM trips WHERE status='active' AND delivered_at IS NULL"):
        plan = latest_plan(t["id"])
        if not plan:
            continue
        v = db.vehicle(t["vehicle_id"])
        done_km = (t.get("gps_km") or 0) - plan.get("start_gps_km", 0)
        for i, s in enumerate(plan["stops"]):
            if s["kind"] in ("no_entry_wait",) or s["at_km"] < done_km:
                continue
            if s["at_km"] - done_km <= 10 and _once(f"planstop:{t['id']}:{i}"):
                what = {"break": "chai break", "refuel": "diesel aur break", "sleep": "raat ka aaraam"}.get(
                    s["kind"], "khana aur aaraam")
                where = f", {s['place'].split(' (')[0]} pe" if s.get("place") else ""
                await bus.send(to_driver(v, f"⏸️ {what} in ~{s['at_km'] - done_km:.0f} km",
                                         speak=f"Boss, {s['at_km'] - done_km:.0f} kilometre aage{where} {what} plan hai. Wahan rukte hain."))
            break
        exp = expected_km_at(plan, now)
        if exp is None or exp >= plan["route"]["km"] and done_km >= plan["route"]["km"] - 2:
            continue
        speed = plan["route"]["km"] / max(plan["driving_h"], 0.1)
        delay_h = (exp - done_km) / speed
        if delay_h >= 1.0 and _once(f"plandelay:{t['id']}:{int(delay_h)}"):
            new_eta = datetime.fromisoformat(plan["arrive_iso"]) + timedelta(hours=delay_h)
            f = db.fleet(v["fleet_id"])
            o = ceo.owner_out(f, f"⏳ {v['reg_label']} is ~{delay_h:.1f} h behind plan ({t['origin']} → {t['destination']}). "
                                 f"New ETA ≈ {_ist(new_eta)}. Ask me to re-plan if the unloading window is at risk.")
            if o:
                await bus.send(o)
            db.log_agent("ceo:planner", f"driver:{v['code']}", "behind_plan", {"delay_h": round(delay_h, 1)})


def expected_km_at(plan: dict, t: datetime) -> float | None:
    """Where the plan expected the truck to be (km from start) at time t, ignoring stops' exact positions."""
    dep, arr = datetime.fromisoformat(plan["depart_iso"]), datetime.fromisoformat(plan["arrive_iso"])
    if t <= dep:
        return 0.0
    if t >= arr:
        return plan["route"]["km"]
    # walk the timeline: drive between stops at the plan's average driving speed
    speed = plan["route"]["km"] / max(plan["driving_h"], 0.1)
    km, cur = 0.0, dep
    stops = sorted(plan["stops"], key=lambda s: s["at_km"])
    for s in stops:
        drive_h = (s["at_km"] - km) / speed
        if cur + timedelta(hours=drive_h) >= t:
            return km + (t - cur).total_seconds() / 3600 * speed
        cur += timedelta(hours=drive_h)
        km = s["at_km"]
        if cur + timedelta(minutes=s["minutes"]) >= t:
            return km
        cur += timedelta(minutes=s["minutes"])
    return min(plan["route"]["km"], km + (t - cur).total_seconds() / 3600 * speed)
