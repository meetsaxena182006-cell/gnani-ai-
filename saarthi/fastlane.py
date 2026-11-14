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


def owner_papers(fleet: dict, hi: bool) -> str:
    """Expired / expiring-in-30-days documents of the whole fleet, straight from the database."""
    from datetime import date
    today = date.today().isoformat()
    in30 = date.fromordinal(date.today().toordinal() + 30).isoformat()
    rows = db.q("SELECT v.code, d.doc_type, d.expiry_date FROM documents d JOIN vehicles v ON v.id=d.vehicle_id "
                "WHERE v.fleet_id=? AND d.expiry_date<=? ORDER BY d.expiry_date", (fleet["id"], in30))
    if not rows:
        return "अगले 30 दिन में किसी गाड़ी के कागज़ ख़त्म नहीं हो रहे।" if hi else "No documents expire in the next 30 days."
    gone = [r for r in rows if r["expiry_date"] < today]
    soon = [r for r in rows if r["expiry_date"] >= today]
    if hi:
        out = []
        if gone:
            out.append("ख़त्म हो चुके: " + ", ".join(f"{r['code']} का {r['doc_type']} ({r['expiry_date']})" for r in gone)
                       + " — यह गाड़ी नया लोड नहीं ले सकती जब तक renew न हो")
        if soon:
            out.append("30 दिन में ख़त्म: " + ", ".join(f"{r['code']} का {r['doc_type']} ({r['expiry_date']})" for r in soon))
        return "। ".join(out) + "।"
    out = []
    if gone:
        out.append("Expired: " + ", ".join(f"{r['code']} {r['doc_type']} ({r['expiry_date']})" for r in gone))
    if soon:
        out.append("Expiring within 30 days: " + ", ".join(f"{r['code']} {r['doc_type']} ({r['expiry_date']})" for r in soon))
    return ". ".join(out) + "."


async def owner_fast(fleet: dict, text: str) -> str | None:
    """Instant answer for a route/trip-cost question, or None to let the AI agent handle it."""
    import os
    if os.getenv("FASTLANE", "1") != "1":
        return None
    q = parse_route(text)
    if not q:
        if _PAPERS.search(text) and not _cities_in(text):
            return owner_papers(fleet, _hindi(text))
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


# ---------------- driver fast lane: the questions a driver asks most, answered by code (no LLM round trip) ----------------
_BREAKDOWN = re.compile(r"(tyre|tair|टायर|puncture|pancture|पंक्चर|पंचर|burst|फट|phat|breakdown|ब्रेकडाउन|kharab|ख़राब|खराब|"
                        r"band ho gay|बंद हो गई|engine|इंजन|start nahi|स्टार्ट नहीं)", re.I)
_EMERGENCY = re.compile(r"(accident|एक्सीडेंट|दुर्घटना|chot|चोट|khoon|खून|ambulance|एम्बुलेंस|aag|आग(?![ऀ-ॿ])|"
                        r"loot|लूट|chori|चोरी)", re.I)   # "आग" (fire) must not match "आगे" (ahead)
_WEATHER = re.compile(r"(mausam|मौसम|barish|baarish|बारिश|kohra|कोहरा|fog|weather|dhund|धुंध|toofan|तूफ़ान)", re.I)
_FATIGUE = re.compile(r"(neend|नींद|thak|थक|sleepy|jhapki|झपकी|aankh band|आँख बंद)", re.I)
_EMPTY = re.compile(r"(khali|khaali|खाली|wapsi ka load|वापसी का लोड|next load|agla load|अगला लोड|koi kaam|कोई काम)", re.I)
_PAPERS = re.compile(r"(kagaz|kaagaz|कागज़|कागज|papers|documents|puc|insurance|इंश्योरेंस|permit|परमिट|fitness|फिटनेस)", re.I)
_MECHANIC = re.compile(r"(mechanic|मैकेनिक|मिस्त्री|mistri|garage|गैराज)", re.I)
_SKY_HI = {"clear": "साफ़", "mostly clear": "लगभग साफ़", "partly cloudy": "हल्के बादल", "overcast": "बादल छाए",
           "fog": "कोहरा", "drizzle": "बूंदाबांदी", "heavy drizzle": "तेज़ बूंदाबांदी", "rain": "बारिश",
           "heavy rain": "तेज़ बारिश", "showers": "बौछारें", "violent showers": "तेज़ बौछारें",
           "thunderstorm": "आंधी-तूफ़ान", "thunderstorm+hail": "तूफ़ान और ओले"}
_WARN_HI = {"FOG": "कोहरा है, धीरे चलिए, फ़ॉग लाइट जलाइए", "HEAVY RAIN": "तेज़ बारिश, ब्रेक की दूरी दोगुनी",
            "THUNDERSTORM": "तूफ़ान, हो सके तो सुरक्षित जगह रुकिए", "HEAT": "गर्मी है, टायर प्रेशर और पानी चेक कीजिए",
            "STRONG WIND": "तेज़ हवा, खाली गाड़ी सँभाल के", "Rain likely": "अगले कुछ घंटों में बारिश हो सकती है"}


async def driver_fast(v: dict, text: str) -> str | None:
    """Instant code answers for the top driver needs (Hindi/Hinglish/English). Other languages and anything
    unclear go to the AI agent."""
    import os
    if os.getenv("FASTLANE", "1") != "1" or (v.get("lang") or "hi-IN") not in ("hi-IN", "en-IN"):
        return None
    if _EMERGENCY.search(text):
        return None            # emergencies: the full agent (severity, 112, owner alert) handles it
    from .agents.driver import Turn, make_tools
    turn = Turn(text)
    tools = {t.name: t.fn for t in make_tools(v["id"], turn)}
    try:
        if _BREAKDOWN.search(text) and not re.search(r"\d", text):   # "puncture repair 300" is an expense, not a breakdown
            r = await asyncio.wait_for(tools["report_breakdown"](problem=text, severity="high"), 15)
            say = turn.final_override or r.get("say", "")
            near = r.get("nearest_help") or []
            if near and "kilometre door" not in say:
                say += f" Sabse paas: {near[0]['name']}, {near[0]['km']} km."
            return say
        if _FATIGUE.search(text):
            r = await asyncio.wait_for(tools["find_help"](kind="dhaba"), 6)
            res = r.get("results") or []
            say = "Boss, neend mein gaadi bilkul nahi. 15-20 minute ruk ke chai pi lijiye, mooh pe paani maariye."
            if res:
                say += f" Sabse paas {res[0]['name']} hai, {res[0]['km']} km pe."
            return say
        if _EMPTY.search(text):
            r = await asyncio.wait_for(tools["find_next_load"](), 20)
            return r.get("say") or None
        if _WEATHER.search(text):
            w = await asyncio.wait_for(tools["weather_now"](), 10)
            if w.get("error"):
                return None
            sky = _SKY_HI.get(w.get("now"), w.get("now"))
            say = f"अभी यहाँ मौसम {sky} है, {round(w['temp_c'])}°C"
            if w.get("wind_kmph") is not None:
                say += f", हवा {round(w['wind_kmph'])} km/h"
            warns = [next((h for k, h in _WARN_HI.items() if x.startswith(k)), x) for x in w.get("warnings", [])]
            say += "। " + ("; ".join(warns) + "।" if warns else "कोई ख़तरा नहीं, आराम से चलिए।")
            return say
        if _PAPERS.search(text):
            from datetime import date
            docs = await tools["my_documents"]()
            if not docs:
                return None
            bad = [d for d in docs if d["status"] == "EXPIRED"]
            in30 = date.fromordinal(date.today().toordinal() + 30).isoformat()
            soon = [d for d in docs if d["status"] != "EXPIRED" and d["expiry_date"] <= in30]
            if bad:
                say = "Dhyaan dijiye: " + ", ".join(f"{d['doc_type']} {d['expiry_date']} ko khatam ho gaya" for d in bad) + ". Pehle renew karwaiye."
            elif soon:
                say = "Sab kaagaz chalu hain, par " + ", ".join(f"{d['doc_type']} {d['expiry_date']} ko khatam hoga" for d in soon) + "."
            else:
                say = "Saare kaagaz valid hain: " + ", ".join(f"{d['doc_type']} {d['expiry_date']} tak" for d in docs[:4]) + "."
            return say
        if _MECHANIC.search(text):
            r = await asyncio.wait_for(tools["find_help"](kind="mechanic"), 10)
            res = r.get("results") or []
            if not res:
                return None
            return f"Sabse paas mechanic: {res[0]['name']}, {res[0]['km']} km. List aapke phone pe bhej di hai."
    except Exception as e:
        log.warning("driver fast lane failed (%s); handing to the agent", e)
    return None


async def prewarm_once() -> None:
    f = db.one("SELECT id FROM fleets LIMIT 1")
    if not f:
        return
    for v in db.q("SELECT last_lat, last_lon FROM vehicles WHERE last_lat IS NOT NULL"):   # help near each truck
        for kind in ("mechanic", "tyre", "dhaba", "fuel"):
            try:
                await asyncio.wait_for(geo.nearby(kind, v["last_lat"], v["last_lon"]), 20)
            except Exception:
                pass
            await asyncio.sleep(1.5)   # Nominatim usage policy: max 1 request per second
        try:
            await asyncio.wait_for(geo.weather(v["last_lat"], v["last_lon"]), 15)
        except Exception:
            pass
    for o, d in WARM_ROUTES:
        try:
            await asyncio.wait_for(planner.plan_request(f["id"], o, d), 30)
        except Exception as e:
            log.info("prewarm %s→%s skipped: %s", o, d, str(e)[:80])
        await asyncio.sleep(2)     # be gentle with the free public map/weather servers


async def prewarm_forever(every_min: int = 25) -> None:
    await asyncio.sleep(5)
    while True:
        import time
        t = time.time()
        await prewarm_once()
        log.info("fast lane: %d common routes warmed in %.0f s", len(WARM_ROUTES), time.time() - t)
        await asyncio.sleep(every_min * 60)
