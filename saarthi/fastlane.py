"""Fast lane: instant answers for the questions owners ask most, without waiting for an LLM round trip.

Route questions ("Jaipur se Pune 12 ton, bhada 95000") are answered straight from the trip planner. The planner's
live inputs (OSRM route, NHAI tolls, Open-Meteo / MET Norway weather) are kept warm in memory by `prewarm_forever`,
so a common route answers in well under a second. Anything the fast lane does not fully understand goes to the
normal AI agent, so nothing is lost.
"""
from __future__ import annotations

import asyncio
import logging
import re

from . import db, geo, planner
from .agents.common import inr

log = logging.getLogger(__name__)

# common freight lanes (synthetic demo fleets run these); refreshed in the background so they are always warm
WARM_ROUTES = [("udaipur", "hyderabad"), ("jaipur", "pune"), ("delhi", "mumbai"), ("ahmedabad", "bengaluru"),
               ("udaipur", "ahmedabad"), ("jaipur", "delhi"), ("mumbai", "bengaluru"), ("udaipur", "mumbai"),
               ("indore", "mumbai"), ("surat", "delhi"), ("chennai", "bengaluru"), ("hyderabad", "chennai"),
               ("kota", "delhi"), ("pune", "hyderabad"), ("delhi", "kolkata"), ("ahmedabad", "delhi")]

_ROUTE_WORDS = re.compile(r"(\bse\b|से|\bto\b|→|->|भेज|bhej|route|रूट|trip|ट्रिप|plan|प्लान|kitna|कितन|पहुँच|pahunch)", re.I)
_TONS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:टन|ton|tonne|t\b)", re.I)
_FREIGHT = re.compile(r"(?:भाड़ा|भाडा|bhada|bhaada|freight|rate|किराया|kiraya)\D{0,6}([\d,]{4,})", re.I)
_FREIGHT2 = re.compile(r"(?:₹|rs\.?\s*)\s*([\d,]{4,})|([\d,]{4,})\s*(?:mein|में|me|rupaye|रुपये|rs|₹)", re.I)
_PROFIT_Q = re.compile(r"(profit|munafa|munaafa|मुनाफ़ा|मुनाफा|fayda|फ़ायदा|quote|margin)", re.I)
_VEH = re.compile(r"\b([A-Z]{2}-T\d+)\b", re.I)
_OTHER_INTENT = re.compile(r"(खाली|khali|load dhoond|लोड ढूं|tracking|ट्रैकिंग|कागज़|kagaz|papers|मुनाफ़ा कुल|p&l|"
                           r"महीने|mahine|month|book|बुक)", re.I)


def _cities_in(text: str) -> list[str]:
    low = text.lower()
    hits = []
    for key, (hi, _, _) in geo.CITY.items():
        for name in (key, hi):
            for m in re.finditer(re.escape(name), low):
                hits.append((m.start(), key))
    hits.sort()
    out = []
    for _, k in hits:
        k = "bengaluru" if k == "bangalore" else k
        if k not in out:
            out.append(k)
    return out


def parse_route(text: str) -> dict | None:
    if not _ROUTE_WORDS.search(text) or _OTHER_INTENT.search(text):
        return None
    cities = _cities_in(text)
    if len(cities) != 2:
        return None
    t = _TONS.search(text)
    f = _FREIGHT.search(text)
    freight = float(f.group(1).replace(",", "")) if f else None
    if freight is None:
        f2 = _FREIGHT2.search(text)
        if f2:
            freight = float((f2.group(1) or f2.group(2)).replace(",", ""))
    if freight is None and _PROFIT_Q.search(text):
        return None          # asks for profit but no price we can read: the AI agent asks / works it out
    v = _VEH.search(text)
    return {"origin": cities[0], "destination": cities[1], "tonnes": float(t.group(1)) if t else None,
            "freight": freight, "vehicle": v.group(1).upper() if v else None}


def _hindi(text: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" for ch in text)


def route_reply(plan: dict, q: dict, label: str, hi: bool) -> str:
    stops = [s for s in plan.get("stops", []) if s["kind"] in ("break", "meal_rest", "meal_rest+refuel", "refuel", "sleep")]
    sleeps = sum(1 for s in stops if s["kind"] == "sleep")
    w = plan.get("weather") or {}
    cost = plan.get("cost") or {}
    o, d = q["origin"].title(), q["destination"].title()
    if hi:
        o, d = geo.CITY[q["origin"]][0], geo.CITY[q["destination"]][0]
        msg = (f"{o} → {d}{label}: {round(plan['route']['km']):,} km, कुल {plan['total_h']} घंटे "
               f"(ड्राइविंग {plan['driving_h']} घंटे, {len(stops)} स्टॉप, {sleeps} बार नींद)। "
               f"टोल {inr(plan['route']['tolls_estimate'])}, डीज़ल {inr(cost.get('diesel', 0))}, कुल खर्च {inr(plan['total_cost'])}।")
        if plan.get("profit") is not None:
            msg += f" भाड़ा {inr(plan['freight'])} पर मुनाफ़ा {inr(plan['profit'])} ({plan['margin_pct']}%)।"
        msg += f" सबसे अच्छा: {plan['depart']} निकलें, {plan['arrive']} पहुँचें।"
        if w:
            sky = "कोहरे का ख़तरा" if w.get("fog") else ("कुछ हिस्सों में हल्की बारिश" if w.get("rain") else "बारिश या कोहरा नहीं")
            msg += f" मौसम (लाइव, {w['source']}): {w['tmin']}–{w['tmax']}°C, {sky}।"
        else:
            msg += " मौसम: लाइव फ़ोरकास्ट अभी नहीं मिला।"
    else:
        msg = (f"{o} → {d}{label}: {round(plan['route']['km']):,} km, total {plan['total_h']} h "
               f"(driving {plan['driving_h']} h, {len(stops)} stops, {sleeps} night sleep). "
               f"Tolls {inr(plan['route']['tolls_estimate'])}, diesel {inr(cost.get('diesel', 0))}, total cost {inr(plan['total_cost'])}.")
        if plan.get("profit") is not None:
            msg += f" At freight {inr(plan['freight'])} profit is {inr(plan['profit'])} ({plan['margin_pct']}%)."
        msg += f" Best: leave {plan['depart']}, arrive {plan['arrive']}."
        if w:
            sky = "fog risk" if w.get("fog") else ("light rain on some stretches" if w.get("rain") else "no rain or fog")
            msg += f" Weather (live, {w['source']}): {w['tmin']}–{w['tmax']}°C, {sky}."
    return msg


_plan_memo: dict = {}


async def owner_fast(fleet: dict, text: str) -> str | None:
    """Instant answer for a route/trip-cost question, or None to let the AI agent handle it."""
    import os
    if os.getenv("FASTLANE", "1") != "1":
        return None
    q = parse_route(text)
    if not q:
        return None
    vtype, label = "truck_10w", ""
    if q["vehicle"]:
        v = db.one("SELECT * FROM vehicles WHERE fleet_id=? AND UPPER(code)=?", (fleet["id"], q["vehicle"]))
        if not v:
            return None
        t = db.active_trip(v["id"])
        if t and q["destination"] in (t["destination"] or "").lower():
            return None          # a live trip already running there: the agent uses the truck's real position
        vtype, label = v["vtype"], f" ({v['code']})"
    else:
        v1 = db.one("SELECT vtype FROM vehicles WHERE fleet_id=? LIMIT 1", (fleet["id"],))
        vtype = v1["vtype"] if v1 else vtype
    import time
    mk = (fleet["id"], q["origin"], q["destination"], vtype, q["tonnes"], q["freight"], fleet.get("diesel_price"))
    hit = _plan_memo.get(mk)
    if hit and time.time() - hit[0] < 600:     # same question within 10 min: same live answer, instantly
        plan = hit[1]
    else:
        try:
            plan = await asyncio.wait_for(planner.plan_request(fleet["id"], q["origin"], q["destination"], vtype,
                                                               q["tonnes"], q["freight"]), 20)
        except Exception as e:
            log.warning("fast lane plan failed (%s); handing to the agent", e)
            return None
        if "error" not in plan:
            if len(_plan_memo) > 300:
                _plan_memo.clear()
            _plan_memo[mk] = (time.time(), plan)
    if "error" in plan:
        return None
    return route_reply(plan, q, label, _hindi(text))


async def prewarm_once() -> None:
    f = db.one("SELECT id FROM fleets LIMIT 1")
    if not f:
        return
    for o, d in WARM_ROUTES:
        try:
            await asyncio.wait_for(planner.plan_request(f["id"], o, d), 30)
        except Exception as e:
            log.info("prewarm %s→%s skipped: %s", o, d, str(e)[:80])
        await asyncio.sleep(2)     # be gentle with the free public map/weather servers


async def prewarm_forever(every_min: int = 45) -> None:
    await asyncio.sleep(5)
    while True:
        import time
        t = time.time()
        await prewarm_once()
        log.info("fast lane: %d common routes warmed in %.0f s", len(WARM_ROUTES), time.time() - t)
        await asyncio.sleep(every_min * 60)
