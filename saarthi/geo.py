"""Location, routing, nearby help and weather. Free sources by default:
OpenStreetMap Nominatim (geocode), OSRM (routes, no traffic), Overpass (mechanics),
Open-Meteo (weather). Google Routes is used for traffic only if GOOGLE_MAPS_API_KEY is set.
"""
from __future__ import annotations

import math

import os

import re
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


# Major freight cities: answered instantly without a map lookup (names in English and Hindi).
CITY = {
    "udaipur": ("उदयपुर", 24.585, 73.712), "ahmedabad": ("अहमदाबाद", 23.0225, 72.5714), "jaipur": ("जयपुर", 26.912, 75.787),
    "indore": ("इंदौर", 22.7196, 75.8577), "surat": ("सूरत", 21.1702, 72.8311), "mumbai": ("मुंबई", 19.076, 72.8777),
    "bhiwandi": ("भिवंडी", 19.2813, 73.0483), "delhi": ("दिल्ली", 28.6139, 77.209), "hyderabad": ("हैदराबाद", 17.385, 78.4867),
    "pune": ("पुणे", 18.5204, 73.8567), "kota": ("कोटा", 25.2138, 75.8648), "vadodara": ("वडोदरा", 22.3072, 73.1812),
    "bhopal": ("भोपाल", 23.2599, 77.4126), "bengaluru": ("बेंगलुरु", 12.9716, 77.5946), "bangalore": ("बैंगलोर", 12.9716, 77.5946),
    "chennai": ("चेन्नई", 13.0827, 80.2707), "kolkata": ("कोलकाता", 22.5726, 88.3639), "nagpur": ("नागपुर", 21.1458, 79.0882),
    "lucknow": ("लखनऊ", 26.8467, 80.9462), "kanpur": ("कानपुर", 26.4499, 80.3319), "ludhiana": ("लुधियाना", 30.901, 75.8573),
    "jodhpur": ("जोधपुर", 26.2389, 73.0243), "ajmer": ("अजमेर", 26.4499, 74.6399), "chittorgarh": ("चित्तौड़गढ़", 24.8887, 74.6269),
    "nashik": ("नासिक", 19.9975, 73.7898), "rajkot": ("राजकोट", 22.3039, 70.8022), "raipur": ("रायपुर", 21.2514, 81.6296),
    "patna": ("पटना", 25.5941, 85.1376), "guwahati": ("गुवाहाटी", 26.1445, 91.7362), "kochi": ("कोच्चि", 9.9312, 76.2673),
    "coimbatore": ("कोयंबटूर", 11.0168, 76.9558), "visakhapatnam": ("विशाखापत्तनम", 17.6868, 83.2185),
    "gurgaon": ("गुड़गांव", 28.4595, 77.0266), "noida": ("नोएडा", 28.5355, 77.391), "agra": ("आगरा", 27.1767, 78.0081),
    "gwalior": ("ग्वालियर", 26.2183, 78.1828), "kandla": ("कांडला", 23.0333, 70.2167), "mundra": ("मुंद्रा", 22.8397, 69.7203),
    "bhilwara": ("भीलवाड़ा", 25.3463, 74.6364), "kishangarh": ("किशनगढ़", 26.5893, 74.8545),
}
_geo_cache: dict[str, dict | None] = {}


def city_lookup(place: str) -> dict | None:
    q = (place or "").strip().lower()
    for key, (hi, lat, lon) in CITY.items():
        if q == key or q == hi or q.startswith(key) or q.startswith(hi):
            return {"name": key.title(), "lat": lat, "lon": lon}
    return None


async def geocode(place: str) -> dict | None:
    hit = city_lookup(place)
    if hit:
        return hit
    k = (place or "").strip().lower()
    if k in _geo_cache:
        return _geo_cache[k]
    res = await _geocode_live(place)
    _geo_cache[k] = res
    return res


async def _geocode_live(place: str) -> dict | None:
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


OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]


async def _overpass(c, query: str, mirrors: int = 4):
    """POST to the first Overpass mirror that answers."""
    err = None
    for url in OVERPASS[:mirrors]:
        try:
            r = await c.post(url, data={"data": query}, headers={"User-Agent": "saarthi-fleet-demo/1.0"})
            if r.status_code == 200 and r.text.lstrip().startswith("{"):
                return r
            err = RuntimeError(f"{url} HTTP {r.status_code}")
        except Exception as e:
            err = e
    raise err


NOMINATIM_PHRASES = {"mechanic": ["car repair", "mechanic", "garage"], "tyre": ["tyres", "car repair"],
                     "fuel": ["fuel"], "hospital": ["hospital", "clinic"], "police": ["police"],
                     "dhaba": ["restaurant", "fast food"]}
_overpass_down_until = 0.0
_places_cache: dict[str, tuple[float, list]] = {}


async def _nominatim(kind: str, lat: float, lon: float, radius_m: int, limit: int) -> list[dict]:
    """Backup place search (OpenStreetMap Nominatim, special phrases inside a box around the truck)."""
    d = max(0.05, radius_m / 111000)
    box = f"{lon - d},{lat + d},{lon + d},{lat - d}"
    els, seen = [], set()
    async with httpx.AsyncClient(headers={"User-Agent": "saarthi-fleet-demo/1.0"}, timeout=httpx.Timeout(10.0)) as c:
        for phrase in NOMINATIM_PHRASES.get(kind, NOMINATIM_PHRASES["mechanic"]):
            r = await c.get("https://nominatim.openstreetmap.org/search",
                            params={"q": phrase, "format": "jsonv2", "limit": 20, "viewbox": box, "bounded": 1,
                                    "extratags": 1})
            if r.status_code != 200:
                continue
            for x in r.json():
                if x.get("osm_id") in seen:
                    continue
                seen.add(x.get("osm_id"))
                ex = x.get("extratags") or {}
                els.append({"lat": float(x["lat"]), "lon": float(x["lon"]),
                            "tags": {"name": x.get("name") or phrase.title(), "phone": ex.get("phone") or ex.get("contact:phone")}})
            if len(els) >= limit:
                break
    return rank_places(els, lat, lon, limit)


async def nearby(kind: str, lat: float, lon: float, radius_m: int = 15000, limit: int = 5) -> list[dict]:
    """Nearest help. Overpass first; when the public Overpass servers are overloaded, Nominatim (cached 15 min)."""
    import time as _t
    global _overpass_down_until
    key = f"{kind}:{round(lat, 2)}:{round(lon, 2)}:{radius_m}"
    hit = _places_cache.get(key)
    # places don't move: keep a found list 6 h; a failed/empty search only 1 min so the next ask retries
    if hit and _t.time() - hit[0] < (6 * 3600 if hit[1] else 60):
        return hit[1][:limit]
    res: list[dict] = []
    if _t.time() > _overpass_down_until:
        try:
            async with httpx.AsyncClient(headers=UA, timeout=httpx.Timeout(6.0)) as c:
                r = await _overpass(c, build_overpass_query(kind, lat, lon, radius_m), mirrors=2)
                res = rank_places(r.json().get("elements", []), lat, lon, limit)
        except Exception:
            _overpass_down_until = _t.time() + 600
    if not res:
        try:
            res = await _nominatim(kind, lat, lon, radius_m, limit)
        except Exception:
            res = []
    if res:
        _places_good[key] = (_t.time(), res)
    else:   # public servers busy right now: the last real result for this spot (shops don't move) beats nothing
        good = _places_good.get(key)
        if good and _t.time() - good[0] < 48 * 3600:
            res = good[1]
    res = [{**x, "name": clean_place_name(x.get("name"), kind)} for x in res]
    _places_cache[key] = (_t.time(), res)
    return res[:limit]


KIND_LABEL = {"mechanic": "Mechanic / garage", "tyre": "Tyre / puncture shop", "fuel": "Petrol pump",
              "hospital": "Hospital", "police": "Police station", "dhaba": "Dhaba / restaurant"}
_JUNK = re.compile(r"(admin|\b(test|asdf|qwer|xxx|unnamed|untitled)\b|^[a-z]{1,2}$|^\W*$)", re.I)


def clean_place_name(name: str | None, kind: str = "") -> str:
    """OpenStreetMap sometimes has junk names (a mapper's username, 'test'): say what the place is instead."""
    n = (name or "").strip()
    if not n or _JUNK.search(n):
        return KIND_LABEL.get(kind, "Shop") + " (map pin)"
    return n


_places_good: dict = {}


def _load_places_seed() -> None:
    """Real OpenStreetMap results for the demo trucks' spots, saved in the repo: a fresh server can name the
    nearest mechanic / dhaba at once even while the public map servers are busy. Live searches replace them."""
    import json as _json
    import time as _t
    from pathlib import Path
    f = Path(__file__).resolve().parent.parent / "data" / "places_seed.json"
    try:
        for k, v in _json.loads(f.read_text(encoding="utf-8")).items():
            _places_good.setdefault(k, (_t.time() - 3600, v))
    except Exception:
        pass


_load_places_seed()


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
_route_cache: dict = {}


async def route(o_lat: float, o_lon: float, d_lat: float, d_lon: float) -> dict:
    """Live route, kept in memory for 6 h (roads don't change by the minute; traffic is only valid when fresh)."""
    import time as _t
    k = (round(o_lat, 2), round(o_lon, 2), round(d_lat, 2), round(d_lon, 2))
    hit = _route_cache.get(k)
    if hit and _t.time() - hit[0] < 6 * 3600:
        return hit[1]
    res = await _route_live(o_lat, o_lon, d_lat, d_lon)
    if res.get("routes"):
        _route_cache[k] = (_t.time(), res)
    return res


async def _route_live(o_lat: float, o_lon: float, d_lat: float, d_lon: float) -> dict:
    if settings.google_maps_key:
        try:
            return await _google_route(o_lat, o_lon, d_lat, d_lon)
        except Exception as e:  # fall back to OSRM
            fallback_reason = str(e)
        else:
            fallback_reason = ""
    else:
        fallback_reason = "no GOOGLE_MAPS_API_KEY"
    if os.getenv("TOMTOM_API_KEY"):          # free tier (2,500 requests/day): live traffic, truck routing
        try:
            return await _tomtom_route(o_lat, o_lon, d_lat, d_lon)
        except Exception as e:
            fallback_reason = f"TomTom failed: {e}"
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


async def _tomtom_route(o_lat, o_lon, d_lat, d_lon) -> dict:
    url = f"https://api.tomtom.com/routing/1/calculateRoute/{o_lat},{o_lon}:{d_lat},{d_lon}/json"
    params = {"key": os.getenv("TOMTOM_API_KEY"), "traffic": "true", "travelMode": "truck", "maxAlternatives": 1,
              "computeTravelTimeFor": "all", "routeRepresentation": "polyline"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        js = r.json()
    routes = []
    for rt in js.get("routes", []):
        sm = rt["summary"]
        coords = [(p["latitude"], p["longitude"]) for leg in rt.get("legs", []) for p in leg.get("points", [])]
        routes.append({
            "distance_km": round(sm["lengthInMeters"] / 1000, 1),
            "duration_h": round(sm["travelTimeInSeconds"] / 3600, 1),
            "truck_duration_h": round(sm["travelTimeInSeconds"] / 3600, 1),
            "traffic_delay_min": round(sm.get("trafficDelayInSeconds", 0) / 60),
            "sample_points": [coords[int(i * (len(coords) - 1) / 4)] for i in range(5)] if coords else [],
            "_coords": coords,
        })
    return {"source": "TomTom (live traffic, truck routing)", "routes": routes, "traffic": "live"}


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

# ---------------- Open-Meteo with cache (shared cloud IPs get rate-limited) + MET Norway backup ----------------
_meteo_cache: dict[str, tuple[float, dict]] = {}


async def _meteo(params: dict, ttl: float = 900) -> dict:
    import time as _t
    p = dict(params)
    p["latitude"], p["longitude"] = round(float(p["latitude"]), 2), round(float(p["longitude"]), 2)
    key = repr(sorted(p.items()))
    hit = _meteo_cache.get(key)
    if hit and _t.time() - hit[0] < ttl:
        return hit[1]
    last = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(headers=UA, timeout=TIMEOUT) as c:
                r = await c.get("https://api.open-meteo.com/v1/forecast", params=p)
                r.raise_for_status()
                js = r.json()
            _meteo_cache[key] = (_t.time(), js)
            if len(_meteo_cache) > 2000:
                _meteo_cache.clear()
            return js
        except Exception as e:
            last = e
            import asyncio as _aio
            await _aio.sleep(1.5)
    if hit:                      # stale is better than nothing
        return hit[1]
    raise last


_MET_CODE = [("thunder", 95), ("fog", 45), ("heavyrain", 65), ("heavysleet", 65), ("rainshowers", 80), ("rain", 61),
             ("sleet", 61), ("snow", 71), ("cloudy", 3), ("partlycloudy", 2), ("fair", 1), ("clearsky", 0)]


async def _met_norway(lat: float, lon: float) -> dict:
    """Backup weather (api.met.no, free, no key): shaped like Open-Meteo's 'current' block."""
    async with httpx.AsyncClient(headers={"User-Agent": "saarthi-fleet-demo/1.0 github.com/meetsaxena182006-cell/gnani-ai-"},
                                 timeout=TIMEOUT) as c:
        r = await c.get("https://api.met.no/weatherapi/locationforecast/2.0/compact",
                        params={"lat": round(lat, 3), "lon": round(lon, 3)})
        r.raise_for_status()
        ts = r.json()["properties"]["timeseries"][0]["data"]
    det = ts["instant"]["details"]
    nxt = ts.get("next_1_hours") or ts.get("next_6_hours") or {}
    sym = (nxt.get("summary") or {}).get("symbol_code", "")
    code = next((c for k, c in _MET_CODE if k in sym), 1)
    return {"current": {"temperature_2m": det.get("air_temperature"), "weather_code": code,
                        "wind_speed_10m": round((det.get("wind_speed") or 0) * 3.6, 1),
                        "precipitation": (nxt.get("details") or {}).get("precipitation_amount", 0),
                        "visibility": 500 if code == 45 else None},
            "hourly": {}, "source": "MET Norway"}


async def weather(lat: float, lon: float) -> dict:
    params = {
        "latitude": lat, "longitude": lon, "timezone": "Asia/Kolkata", "forecast_days": 1,
        "current": "temperature_2m,precipitation,weather_code,wind_speed_10m,visibility",
        "hourly": "precipitation_probability,visibility,weather_code",
    }
    import asyncio as _aio
    import time as _t
    global _meteo_down_until
    js = None
    if _t.time() >= _meteo_down_until:
        try:
            js = await _aio.wait_for(_meteo(params, ttl=600), 5)
            src = "Open-Meteo"
        except Exception:
            _meteo_down_until = _t.time() + 600
    if js is None:
        k = ("now", round(lat, 2), round(lon, 2))
        hit = _met_hourly_cache.get(k)
        if hit and _t.time() - hit[0] < 600:
            js = hit[1]
        else:
            js = await _met_norway(lat, lon)
            _met_hourly_cache[k] = (_t.time(), js)
        src = "MET Norway"
    out = assess_weather(js)
    out["source"] = src
    return out


async def forecast_hourly(lat: float, lon: float, days: int = 3) -> dict:
    """Raw hourly forecast (IST) for a point: used by the trip planner to test many departure times offline."""
    params = {"latitude": lat, "longitude": lon, "timezone": "Asia/Kolkata", "forecast_days": days,
              "hourly": "precipitation_probability,precipitation,visibility,weather_code,temperature_2m,wind_speed_10m"}
    import asyncio as _aio
    import time as _t
    global _meteo_down_until
    if _t.time() >= _meteo_down_until:
        try:
            return await _aio.wait_for(_meteo(params, ttl=7200), 6)
        except Exception:
            _meteo_down_until = _t.time() + 600   # Open-Meteo refusing us (429 from shared cloud IPs): use backup
    return await _met_norway_hourly(lat, lon)


_meteo_down_until = 0.0


_met_hourly_cache: dict = {}


async def _met_norway_hourly(lat: float, lon: float) -> dict:
    """Backup hourly forecast from MET Norway (free, no key), shaped like Open-Meteo's 'hourly' block (IST times)."""
    import time as _t
    from datetime import datetime as _dt, timedelta as _td
    k = (round(lat, 2), round(lon, 2))
    hit = _met_hourly_cache.get(k)
    if hit and _t.time() - hit[0] < 7200:
        return hit[1]
    async with httpx.AsyncClient(headers={"User-Agent": "saarthi-fleet-demo/1.0 github.com/meetsaxena182006-cell/gnani-ai-"},
                                 timeout=httpx.Timeout(8.0)) as c:
        r = await c.get("https://api.met.no/weatherapi/locationforecast/2.0/compact",
                        params={"lat": round(lat, 3), "lon": round(lon, 3)})
        r.raise_for_status()
        series = r.json()["properties"]["timeseries"]
    h = {"time": [], "precipitation": [], "weather_code": [], "temperature_2m": [], "wind_speed_10m": [],
         "visibility": [], "precipitation_probability": []}
    for row in series[:72]:
        t = _dt.fromisoformat(row["time"].replace("Z", "+00:00")) + _td(hours=5, minutes=30)
        det = row["data"]["instant"]["details"]
        nxt = row["data"].get("next_1_hours") or row["data"].get("next_6_hours") or {}
        code = next((cc for kk, cc in _MET_CODE if kk in (nxt.get("summary") or {}).get("symbol_code", "")), 1)
        h["time"].append(t.strftime("%Y-%m-%dT%H:00"))
        h["precipitation"].append((nxt.get("details") or {}).get("precipitation_amount", 0))
        h["weather_code"].append(code)
        h["temperature_2m"].append(det.get("air_temperature"))
        h["wind_speed_10m"].append(round((det.get("wind_speed") or 0) * 3.6, 1))
        h["visibility"].append(500 if code == 45 else None)
        h["precipitation_probability"].append(None)
    js = {"hourly": h, "source": "MET Norway"}
    _met_hourly_cache[k] = (_t.time(), js)
    return js


async def overpass_along(points: list[tuple[float, float]], kinds: tuple[str, ...] = ("fuel", "dhaba"),
                         radius_m: int = 3000) -> list[list[dict]]:
    """Fuel pumps / dhabas near each planned stop point, in ONE Overpass request (kept 12 h)."""
    if not points:
        return []
    import time as _t
    ck = (tuple((round(a, 2), round(b, 2)) for a, b in points), kinds, radius_m)
    hit = _along_cache.get(ck)
    if hit and _t.time() - hit[0] < 12 * 3600:
        return hit[1]
    out = await _overpass_along_live(points, kinds, radius_m)
    _along_cache[ck] = (_t.time(), out)
    return out


_along_cache: dict = {}


async def _overpass_along_live(points, kinds, radius_m) -> list[list[dict]]:
    tags = [t for k in kinds for t in HELP_TAGS[k]]
    body = "".join(f"{t}(around:{radius_m},{lat},{lon});" for lat, lon in points for t in tags)
    async with httpx.AsyncClient(headers=UA, timeout=httpx.Timeout(6.0)) as c:
        r = await _overpass(c, f"[out:json][timeout:10];({body});out center 200;", mirrors=2)
        r.raise_for_status()
        elements = r.json().get("elements", [])
    out = []
    for lat, lon in points:
        ranked = rank_places(elements, lat, lon, limit=50)
        out.append([p for p in ranked if p["distance_km"] <= radius_m / 1000][:3])
    return out


async def forecast_at(lat: float, lon: float, hours_ahead: float) -> dict:
    """Weather expected at a point when the truck reaches it (hourly forecast, up to 48 h)."""
    params = {"latitude": lat, "longitude": lon, "timezone": "Asia/Kolkata", "forecast_days": 2,
              "hourly": "precipitation_probability,precipitation,visibility,weather_code,temperature_2m,wind_speed_10m"}
    js = await _meteo(params)
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
