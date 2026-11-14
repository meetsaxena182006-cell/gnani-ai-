"""NHAI toll plazas (government data) for toll-crossing detection and route toll estimates.

Data: data/toll_plazas_nhai.json, extracted from NHAI's Toll Information System (TIS) via the community
snapshot github.com/geohacker/toll-plazas-india (30-06-2020), Government Open Data License - India.
Only name, location and fee per vehicle class are kept. Rates are from 2019-20 fee notifications, so actual
tolls today are higher: treat as a floor, and use the FASTag statement / driver's answer as the truth.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from .geo import haversine_km

DATA = Path(__file__).resolve().parent.parent / "data" / "toll_plazas_nhai.json"

# Our vehicle profiles -> NHAI fee classes (single journey). "Return within 24h" is 1.5x single by NHAI rules.
CLASS_KEYS = {
    "tempo_lcv": ["lcv_single", ("lcv_multi", 1 / 1.5)],
    "truck_6w": [("bus_multi", 1 / 1.5)],                          # 2-axle truck = bus/truck class
    "truck_10w": ["multiaxle_single", ("multiaxle_multi", 1 / 1.5)],  # 3-axle commercial
    "trailer": ["four_six_axle_single", "hcm_single", ("four_six_axle_multi", 1 / 1.5)],
}


@lru_cache(maxsize=1)
def plazas() -> list[dict]:
    if not DATA.exists():
        return []
    return json.loads(DATA.read_text(encoding="utf-8"))["plazas"]


def expected_fee(plaza: dict, vtype: str) -> float | None:
    rates = plaza.get("rates") or {}
    for key in CLASS_KEYS.get(vtype, CLASS_KEYS["truck_10w"]):
        k, factor = (key, 1.0) if isinstance(key, str) else key
        if rates.get(k):
            return round(rates[k] * factor)
    return None


def nearest(lat: float, lon: float, max_km: float = 50.0) -> tuple[dict, float] | None:
    best = None
    for p in plazas():
        if abs(p["lat"] - lat) > 0.6 or abs(p["lon"] - lon) > 0.6:
            continue
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        if d <= max_km and (best is None or d < best[1]):
            best = (p, d)
    return best


def _point_segment_km(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance from point p to segment a-b (equirectangular approximation; fine at these scales)."""
    lat0 = math.radians((a[0] + b[0]) / 2)
    def xy(q):
        return (q[1] * 111.32 * math.cos(lat0), q[0] * 110.57)
    px, py = xy(p); ax, ay = xy(a); bx, by = xy(b)
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def crossed(prev: tuple[float, float] | None, cur: tuple[float, float], radius_km: float = 0.4) -> list[dict]:
    """Plazas passed between two GPS fixes (or at the current fix)."""
    a = prev or cur
    lo_lat, hi_lat = min(a[0], cur[0]) - 0.02, max(a[0], cur[0]) + 0.02
    lo_lon, hi_lon = min(a[1], cur[1]) - 0.02, max(a[1], cur[1]) + 0.02
    out = []
    for p in plazas():
        if lo_lat <= p["lat"] <= hi_lat and lo_lon <= p["lon"] <= hi_lon:
            if _point_segment_km((p["lat"], p["lon"]), a, cur) <= radius_km:
                out.append(p)
    return out


def on_route(coords_latlon: list[tuple[float, float]], vtype: str, tolerance_km: float = 0.8) -> dict:
    """Toll plazas along a route polyline and the expected total for this vehicle class."""
    if len(coords_latlon) < 2:
        return {"plazas": [], "expected_total": 0, "unknown_rates": 0}
    ck = (hash(tuple(map(tuple, coords_latlon))), vtype, tolerance_km)
    if ck in _on_route_cache:
        return _on_route_cache[ck]
    lats = [c[0] for c in coords_latlon]; lons = [c[1] for c in coords_latlon]
    box = (min(lats) - 0.05, max(lats) + 0.05, min(lons) - 0.05, max(lons) + 0.05)
    # grid index: 0.1-degree cells -> road segments touching them, so each plaza checks only nearby road
    cell = 0.1
    grid: dict = {}
    for i in range(len(coords_latlon) - 1):
        a, b = coords_latlon[i], coords_latlon[i + 1]
        for gx in range(int(math.floor((min(a[0], b[0]) - 0.02) / cell)), int(math.floor((max(a[0], b[0]) + 0.02) / cell)) + 1):
            for gy in range(int(math.floor((min(a[1], b[1]) - 0.02) / cell)), int(math.floor((max(a[1], b[1]) + 0.02) / cell)) + 1):
                grid.setdefault((gx, gy), []).append(i)
    found = []
    for p in plazas():
        if not (box[0] <= p["lat"] <= box[1] and box[2] <= p["lon"] <= box[3]):
            continue
        cand = grid.get((int(math.floor(p["lat"] / cell)), int(math.floor(p["lon"] / cell))))
        if not cand:
            continue
        best_i, best_d = None, 1e9
        for i in cand:
            d = _point_segment_km((p["lat"], p["lon"]), coords_latlon[i], coords_latlon[i + 1])
            if d < best_d:
                best_i, best_d = i, d
        if best_d <= tolerance_km:
            found.append((best_i, p))
    found.sort(key=lambda x: x[0])
    items, total, unknown = [], 0, 0
    for _, p in found:
        fee = expected_fee(p, vtype)
        if fee is None:
            unknown += 1
        else:
            total += fee
        items.append({"name": p["name"], "fee_2019_20": fee})
    res = {"plazas": items, "expected_total": total, "unknown_rates": unknown,
           "note": "NHAI 2019-20 rates; current tolls are higher (annual revisions). Use as a minimum."}
    if len(_on_route_cache) > 500:
        _on_route_cache.clear()
    _on_route_cache[ck] = res
    return res


_on_route_cache: dict = {}
