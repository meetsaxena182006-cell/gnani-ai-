"""Route Oracle: route, live weather, predicted traffic, ETA, cost and risk.

Road geometry: public OSRM (falls back to straight-line x 1.22).
Weather: Open-Meteo (free, no key). Traffic: hour-of-day + corridor model (PREDICTED, not live).
Google Maps Routes API can replace OSRM when a Maps key is configured.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx

from .fleet import CITIES, DIESEL_RS_PER_L, KMPL, TOLL_RS_PER_KM, haversine, lerp
from .tolls import on_route

WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Dense fog",
       51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
       71: "Snow", 80: "Rain showers", 81: "Heavy showers", 82: "Violent showers", 95: "Thunderstorm",
       96: "Thunderstorm + hail", 99: "Severe thunderstorm"}

# Congestion multipliers by hour (truck speed factor, 1.0 = free flow). City-entry restrictions
# (no-entry hours for heavy vehicles) are modelled as a strong slowdown near metros at daytime.
HOUR_FACTOR = [1.0, 1.0, 1.0, 1.0, 0.98, 0.95, 0.9, 0.82, 0.74, 0.72, 0.78, 0.84,
               0.86, 0.85, 0.83, 0.8, 0.76, 0.7, 0.68, 0.74, 0.84, 0.92, 0.97, 1.0]
METROS = {"Delhi", "Mumbai", "Ahmedabad", "Pune", "Surat", "Jaipur", "Indore"}
FREE_FLOW_KMPH = 52.0

_cache: dict = {}


async def osrm_route(a, b):
    key = ("osrm", a, b)
    if key in _cache:
        return _cache[key]
    url = f"https://router.project-osrm.org/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson"
    try:
        async with httpx.AsyncClient(timeout=7.0) as c:
            r = await c.get(url)
            d = r.json()["routes"][0]
            full = [[p[1], p[0]] for p in d["geometry"]["coordinates"]]
            step = max(1, len(full) // 400)   # full geometry for toll matching, ~400 points for the map
            res = {"km": round(d["distance"] / 1000, 1), "coords": full[::step] + [full[-1]], "full": full,
                   "source": "OSRM road network"}
    except Exception:
        pts = [lerp(a, b, i / 12) for i in range(13)]
        res = {"km": round(haversine(a, b) * 1.22, 1), "coords": [list(p) for p in pts],
               "source": "estimated (offline)"}
    _cache[key] = res
    return res


async def weather_at(lat, lng):
    key = ("wx", round(lat, 1), round(lng, 1), datetime.now().hour)
    if key in _cache:
        return _cache[key]
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%.3f&longitude=%.3f"
           "&current=temperature_2m,weather_code,wind_speed_10m,visibility&hourly=precipitation_probability"
           "&forecast_hours=12&timezone=Asia%%2FKolkata" % (lat, lng))
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            d = (await c.get(url)).json()
            cur = d["current"]
            rain = max(d.get("hourly", {}).get("precipitation_probability", [0]) or [0])
            res = {"temp_c": cur["temperature_2m"], "condition": WMO.get(cur["weather_code"], "Unknown"),
                   "code": cur["weather_code"], "wind_kmph": cur["wind_speed_10m"],
                   "visibility_km": round((cur.get("visibility") or 10000) / 1000, 1),
                   "rain_chance_12h": rain, "source": "Open-Meteo live"}
    except Exception:
        res = {"temp_c": None, "condition": "Unavailable", "code": 0, "wind_kmph": None, "visibility_km": None,
               "rain_chance_12h": 0, "source": "offline"}
    _cache[key] = res
    return res


def weather_factor(w):
    code, vis = w.get("code", 0), w.get("visibility_km") or 10
    f = 1.0
    if code in (45, 48) or vis < 1.5:
        f *= 0.7
    elif code >= 61:
        f *= 0.82
    elif code >= 51:
        f *= 0.92
    return f


async def predict_trip(origin: str, dest: str, depart: datetime | None = None, tons: int = 21):
    depart = depart or datetime.now()
    a, b = CITIES[origin], CITIES[dest]
    route = await osrm_route(a, b)
    samples = [route["coords"][int(i * (len(route["coords"]) - 1) / 3)] for i in range(4)]
    wx = await asyncio.gather(*(weather_at(p[0], p[1]) for p in samples))
    wf = min(weather_factor(w) for w in wx)
    km = route["km"]

    def simulate(start: datetime):
        t, done, hours, rest = start, 0.0, 0.0, 0.0
        while done < km:
            f = HOUR_FACTOR[t.hour]
            near_metro = (done < 25 and origin in METROS) or (km - done < 25 and dest in METROS)
            if near_metro and 8 <= t.hour < 22:
                f *= 0.55  # heavy-vehicle no-entry / city congestion
            step = FREE_FLOW_KMPH * f * wf * 0.25
            done += step
            hours += 0.25
            t += timedelta(minutes=15)
            if hours - rest >= 4.5:  # mandatory-style 30 min break every 4.5 h of driving
                hours += 0.5
                rest = hours
                t += timedelta(minutes=30)
        return hours

    eta_h = simulate(depart)
    options = []
    for add in (0, 2, 4, 6, 9, 12):
        s = depart + timedelta(hours=add)
        options.append({"depart": s.strftime("%a %H:%M"), "hours": round(simulate(s), 1)})
    best = min(options, key=lambda o: o["hours"])
    fuel_l = km / (KMPL - (0.2 if tons > 24 else 0))
    tolls = on_route(route.get("full") or route["coords"]) if route["source"].startswith("OSRM") else {"count": 0}
    per_km = round(km * TOLL_RS_PER_KM)
    toll_rs = max(tolls.get("estimate_today", 0), per_km)
    if tolls.get("count"):
        tolls["basis"] = ("NHAI plazas total" if tolls["estimate_today"] >= per_km else
                          f"per-km estimate Rs {TOLL_RS_PER_KM}/km (2020 NHAI list covers {tolls['count']} plazas on this road)")
    cost = {"diesel": round(fuel_l * DIESEL_RS_PER_L), "toll": toll_rs,
            "driver": round(max(1, eta_h / 10) * 900)}
    cost["total"] = sum(cost.values())
    risk = 12 + (25 if wf < 0.8 else 8 if wf < 1 else 0) + (10 if eta_h > 14 else 0) + (6 if dest in METROS else 0)
    return {
        "origin": origin, "dest": dest, "km": km, "route_source": route["source"], "coords": route["coords"],
        "eta_hours": round(eta_h, 1), "arrive": (depart + timedelta(hours=eta_h)).strftime("%a %d %b, %H:%M"),
        "weather": wx, "weather_factor": round(wf, 2), "traffic": "predicted (hour-of-day + metro entry model)",
        "best_departure": best, "departure_options": options, "cost": cost, "risk_score": min(risk, 95),
        "advice": _advice(wx, best, options[0], dest),
        "tolls": tolls if tolls.get("count") else {"count": 0, "plazas": [],
                                                   "source": f"estimate Rs {TOLL_RS_PER_KM}/km (no road geometry)"},
    }


def _advice(wx, best, now, dest):
    tips = []
    bad = [w for w in wx if w["code"] >= 45]
    if bad:
        tips.append(f"{bad[0]['condition']} expected on the corridor - keep speed under 50 km/h, lights on.")
    if best["hours"] < now["hours"] - 0.4:
        tips.append(f"Leaving at {best['depart']} saves {round(now['hours'] - best['hours'], 1)} h versus now.")
    if dest in METROS:
        tips.append(f"{dest} has daytime heavy-vehicle restrictions - plan city entry after 22:00.")
    return tips or ["Clear run expected. Standard breaks every 4.5 hours."]
