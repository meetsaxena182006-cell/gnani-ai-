"""Location, routing, nearby help and weather. Free sources by default:
OpenStreetMap Nominatim (geocode), OSRM (routes, no traffic), Overpass (mechanics),
Open-Meteo (weather). Google Routes is used for traffic only if GOOGLE_MAPS_API_KEY is set.
"""
from __future__ import annotations

import math

import httpx

from .config import settings

UA = {"User-Agent": "saarthi-fleet-demo/0.1"}
TRUCK_FACTOR = 1.4  # loaded trucks average ~40% slower than OSRM car estimates on Indian highways
TIMEOUT = httpx.Timeout(20.0)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def maps_link(lat: float, lon: float) -> str:
    return f"https://maps.google.com/?q={lat:.5f},{lon:.5f}"


async def geocode(place: str) -> dict | None:
    async with httpx.AsyncClient(headers=UA, timeout=TIMEOUT) as c:
        r = await c.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place, "format": "json", "limit": 1, "countrycodes": "in"},
        )
        r.raise_for_status()
        js = r.json()
    if not js:
        return None
    return {"name": js[0]["display_name"], "lat": float(js[0]["lat"]), "lon": float(js[0]["lon"])}


async def reverse(lat: float, lon: float) -> str:
    try:
        async with httpx.AsyncClient(headers=UA, timeout=TIMEOUT) as c:
            r = await c.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "zoom": 14},
            )
            r.raise_for_status()
            return r.json().get("display_name", f"{lat:.4f},{lon:.4f}")
    except Exception:
        return f"{lat:.4f},{lon:.4f}"


# ---------------- nearby help ----------------
HELP_TAGS = {
    "mechanic": ['nwr["shop"="car_repair"]', 'nwr["craft"="mechanic"]', 'nwr["amenity"="vehicle_inspection"]'],
    "tyre": ['nwr["shop"="tyres"]'],
    "fuel": ['nwr["amenity"="fuel"]'],
    "hospital": ['nwr["amenity"="hospital"]', 'nwr["amenity"="clinic"]'],
    "police": ['nwr["amenity"="police"]'],
    "dhaba": ['nwr["amenity"="restaurant"]', 'nwr["amenity"="fast_food"]'],
}


def build_overpass_query(kind: str, lat: float, lon: float, radius_m: int) -> str:
    tags = HELP_TAGS.get(kind, HELP_TAGS["mechanic"])
    if kind == "tyre":  # tyre shops are rare in OSM; include mechanics too
        tags = tags + HELP_TAGS["mechanic"]
    body = "".join(f"{t}(around:{radius_m},{lat},{lon});" for t in tags)
    return f"[out:json][timeout:20];({body});out center 30;"


async def nearby(kind: str, lat: float, lon: float, radius_m: int = 15000, limit: int = 5) -> list[dict]:
    query = build_overpass_query(kind, lat, lon, radius_m)
    async with httpx.AsyncClient(headers=UA, timeout=TIMEOUT) as c:
        r = await c.post("https://overpass-api.de/api/interpreter", data={"data": query})
        r.raise_for_status()
        elements = r.json().get("elements", [])
    return rank_places(elements, lat, lon, limit)


def rank_places(elements: list[dict], lat: float, lon: float, limit: int = 5) -> list[dict]:
    out = []
    for e in elements:
        plat = e.get("lat") or e.get("center", {}).get("lat")
        plon = e.get("lon") or e.get("center", {}).get("lon")
        if plat is None or plon is None:
            continue
        tags = e.get("tags", {})
        out.append({
            "name": tags.get("name") or tags.get("shop") or tags.get("amenity") or "Unnamed",
            "phone": tags.get("phone") or tags.get("contact:phone"),
            "distance_km": round(haversine_km(lat, lon, plat, plon), 1),
            "lat": plat,
            "lon": plon,
            "maps": maps_link(plat, plon),
        })
    out.sort(key=lambda x: x["distance_km"])
    return out[:limit]


# ---------------- routing ----------------
async def route(o_lat: float, o_lon: float, d_lat: float, d_lon: float) -> dict:
    if settings.google_maps_key:
        try:
            return await _google_route(o_lat, o_lon, d_lat, d_lon)
        except Exception as e:  # fall back to OSRM
            fallback_reason = str(e)
        else:
            fallback_reason = ""
    else:
        fallback_reason = "no GOOGLE_MAPS_API_KEY"
    res = await _osrm_route(o_lat, o_lon, d_lat, d_lon)
    res["traffic"] = f"not available ({fallback_reason})"
    return res


async def _osrm_route(o_lat, o_lon, d_lat, d_lon) -> dict:
    url = f"https://router.project-osrm.org/route/v1/driving/{o_lon},{o_lat};{d_lon},{d_lat}"
    async with httpx.AsyncClient(headers=UA, timeout=TIMEOUT) as c:
        r = await c.get(url, params={"alternatives": "true", "overview": "full", "geometries": "geojson"})
        r.raise_for_status()
        js = r.json()
    routes = []
    for rt in js.get("routes", []):
        coords = rt["geometry"]["coordinates"]
        routes.append({
            "distance_km": round(rt["distance"] / 1000, 1),
            "duration_h": round(rt["duration"] / 3600, 1),  # car-speed estimate; trucks are slower
            "truck_duration_h": round(rt["duration"] * TRUCK_FACTOR / 3600, 1),
            "sample_points": _sample(coords, 5),
            "_coords": [(c[1], c[0]) for c in coords],
        })
    return {"source": "OSRM (OpenStreetMap)", "routes": routes,
            "note": "OSRM durations assume car speed; expect trucks to take ~1.4x longer."}


async def _google_route(o_lat, o_lon, d_lat, d_lon) -> dict:
    body = {
        "origin": {"location": {"latLng": {"latitude": o_lat, "longitude": o_lon}}},
        "destination": {"location": {"latLng": {"latitude": d_lat, "longitude": d_lon}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": True,
    }
    headers = {
        "X-Goog-Api-Key": settings.google_maps_key,
        "X-Goog-FieldMask": "routes.distanceMeters,routes.duration,routes.staticDuration,routes.description",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post("https://routes.googleapis.com/directions/v2:computeRoutes", json=body, headers=headers)
        r.raise_for_status()
        js = r.json()
    routes = []
    for rt in js.get("routes", []):
        dur = float(rt["duration"].rstrip("s"))
        static = float(rt.get("staticDuration", rt["duration"]).rstrip("s"))
        routes.append({
            "via": rt.get("description", ""),
            "distance_km": round(rt["distanceMeters"] / 1000, 1),
            "duration_h": round(dur / 3600, 1),
            "traffic_delay_min": round((dur - static) / 60),
        })
    return {"source": "Google Routes (traffic-aware)", "routes": routes, "traffic": "live"}


def _sample(coords: list[list[float]], n: int) -> list[tuple[float, float]]:
    if not coords:
        return []
    if len(coords) <= n:
        return [(c[1], c[0]) for c in coords]
    step = (len(coords) - 1) / (n - 1)
    return [(coords[round(i * step)][1], coords[round(i * step)][0]) for i in range(n)]


# ---------------- weather ----------------
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
       51: "drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "rain", 63: "rain", 65: "heavy rain",
       80: "showers", 81: "showers", 82: "violent showers", 95: "thunderstorm", 96: "thunderstorm+hail",
       99: "thunderstorm+hail"}


async def weather(lat: float, lon: float) -> dict:
    params = {
        "latitude": lat, "longitude": lon, "timezone": "Asia/Kolkata", "forecast_days": 1,
        "current": "temperature_2m,precipitation,weather_code,wind_speed_10m,visibility",
        "hourly": "precipitation_probability,visibility,weather_code",
    }
    async with httpx.AsyncClient(headers=UA, timeout=TIMEOUT) as c:
        r = await c.get("https://api.open-meteo.com/v1/forecast", params=params)
        r.raise_for_status()
        js = r.json()
    return assess_weather(js)


async def forecast_at(lat: float, lon: float, hours_ahead: float) -> dict:
    """Weather expected at a point when the truck reaches it (hourly forecast, up to 48 h)."""
    params = {"latitude": lat, "longitude": lon, "timezone": "Asia/Kolkata", "forecast_days": 2,
              "hourly": "precipitation_probability,precipitation,visibility,weather_code,temperature_2m,wind_speed_10m"}
    async with httpx.AsyncClient(headers=UA, timeout=TIMEOUT) as c:
        r = await c.get("https://api.open-meteo.com/v1/forecast", params=params)
        r.raise_for_status()
        js = r.json()
    return assess_hour(js, hours_ahead)


def assess_hour(js: dict, hours_ahead: float) -> dict:
    from datetime import datetime, timedelta, timezone
    h = js.get("hourly", {})
    times = h.get("time") or []
    if not times:
        return {"warnings": []}
    target = (datetime.now(timezone(timedelta(hours=5, minutes=30))) + timedelta(hours=hours_ahead)).strftime("%Y-%m-%dT%H:00")
    i = times.index(target) if target in times else min(len(times) - 1, max(0, int(hours_ahead)))
    pick = lambda k: (h.get(k) or [None] * len(times))[i]
    cur = {"weather_code": pick("weather_code"), "visibility": pick("visibility"), "precipitation": pick("precipitation"),
           "temperature_2m": pick("temperature_2m"), "wind_speed_10m": pick("wind_speed_10m")}
    out = assess_weather({"current": cur, "hourly": {"precipitation_probability": [pick("precipitation_probability")]}})
    out["at"] = times[i]
    return out


def assess_weather(js: dict) -> dict:
    cur = js.get("current", {})
    hourly = js.get("hourly", {})
    code = cur.get("weather_code")
    warnings = []
    vis = cur.get("visibility")
    if code in (45, 48) or (vis is not None and vis < 1000):
        warnings.append("FOG: low visibility, slow down, fog lights on")
    if code in (65, 82) or (cur.get("precipitation") or 0) > 7:
        warnings.append("HEAVY RAIN: braking distance doubles")
    if code in (95, 96, 99):
        warnings.append("THUNDERSTORM: park in a safe place if possible")
    if (cur.get("temperature_2m") or 0) >= 42:
        warnings.append("HEAT: check tyre pressure and coolant, drink water")
    if (cur.get("wind_speed_10m") or 0) >= 45:
        warnings.append("STRONG WIND: careful with empty/high loads")
    probs = (hourly.get("precipitation_probability") or [])[:6]
    if probs and max(p or 0 for p in probs) >= 70:
        warnings.append("Rain likely in the next few hours")
    return {
        "now": WMO.get(code, f"code {code}"),
        "temp_c": cur.get("temperature_2m"),
        "visibility_m": vis,
        "wind_kmph": cur.get("wind_speed_10m"),
        "warnings": warnings,
    }
