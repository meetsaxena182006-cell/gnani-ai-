"""NHAI toll plazas (government data) along a route - merged from the Saarthi Gnani build.

Data: data/toll_plazas_nhai.json from NHAI's Toll Information System (TIS) via github.com/geohacker/toll-plazas-india
(snapshot 30-06-2020), Government Open Data License - India. Rates are 2019-20 fee notifications; NHAI revises
tolls every April, so we add a labelled revision uplift and treat the result as an estimate (FASTag = the truth).
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "toll_plazas_nhai.json"
REVISION_UPLIFT = 1.30   # ~5%/yr annual revisions 2020-2026 (estimate, labelled in the UI)


@lru_cache(maxsize=1)
def plazas() -> list[dict]:
    if not DATA.exists():
        return []
    return json.loads(DATA.read_text(encoding="utf-8"))["plazas"]


def fee(p: dict) -> int | None:
    """3-axle commercial truck, single journey (falls back to multi-journey / 1.5)."""
    r = p.get("rates") or {}
    if r.get("multiaxle_single"):
        return round(r["multiaxle_single"])
    if r.get("multiaxle_multi"):
        return round(r["multiaxle_multi"] / 1.5)
    return None


def _seg_km(p, a, b):
    lat0 = math.radians((a[0] + b[0]) / 2)
    xy = lambda q: (q[1] * 111.32 * math.cos(lat0), q[0] * 110.57)
    (px, py), (ax, ay), (bx, by) = xy(p), xy(a), xy(b)
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


_cache: dict = {}


def on_route(coords: list, tol_km: float = 0.9) -> dict:
    """coords: [[lat, lng], ...] road polyline -> plazas crossed in order + expected toll."""
    if len(coords) < 2:
        return {"plazas": [], "count": 0, "total_2019_20": 0, "estimate_today": 0}
    key = (len(coords), tuple(coords[0]), tuple(coords[-1]))
    if key in _cache:
        return _cache[key]
    lats = [c[0] for c in coords]
    lngs = [c[1] for c in coords]
    box = (min(lats) - .05, max(lats) + .05, min(lngs) - .05, max(lngs) + .05)
    cell, grid = 0.1, {}
    for i in range(len(coords) - 1):
        a, b = coords[i], coords[i + 1]
        for gx in range(math.floor((min(a[0], b[0]) - .02) / cell), math.floor((max(a[0], b[0]) + .02) / cell) + 1):
            for gy in range(math.floor((min(a[1], b[1]) - .02) / cell), math.floor((max(a[1], b[1]) + .02) / cell) + 1):
                grid.setdefault((gx, gy), []).append(i)
    found = []
    for p in plazas():
        if not (box[0] <= p["lat"] <= box[1] and box[2] <= p["lon"] <= box[3]):
            continue
        cand = grid.get((math.floor(p["lat"] / cell), math.floor(p["lon"] / cell)))
        if not cand:
            continue
        i, d = min(((i, _seg_km((p["lat"], p["lon"]), coords[i], coords[i + 1])) for i in cand), key=lambda x: x[1])
        if d <= tol_km:
            found.append((i, p))
    found.sort(key=lambda x: x[0])
    items = [{"name": p["name"], "lat": p["lat"], "lng": p["lon"], "fee_2019_20": fee(p)} for _, p in found]
    total = sum(x["fee_2019_20"] or 0 for x in items)
    res = {"plazas": items, "count": len(items), "total_2019_20": total,
           "estimate_today": round(total * REVISION_UPLIFT / 10) * 10,
           "source": "NHAI TIS toll plazas (Govt Open Data), 2019-20 rates + ~30% revision estimate"}
    _cache[key] = res
    return res
