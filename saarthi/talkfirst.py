"""Talk first, like a person: Saarthi opens the conversation itself on the typed pages too (the live voice call
has its own loop in companion.py).

- Driver page opened -> a greeting built from the truck's real state (trip, hours at the wheel, live weather,
  unreported toll, expired papers, empty truck) that ends with a question or an offer.
- Driver quiet -> short nudges on a growing gap (weather, toll, papers, rest, family chat), at most MAX_NUDGES
  until the driver talks again.
- Office opened -> the CEO Agent briefs the owner first (Sentinel headline, what needs attention, an idle truck
  it can find a load for) and keeps nudging while the owner is quiet.
- "Haan" to an offer (find a load / chai break) runs it straight away.
- Phone calls are SIMULATED in this demo (real calling needs WhatsApp Business / telephony keys) and say so.

Everything here is code over the database and live caches: no LLM wait, answers in well under a second.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

from . import db

log = logging.getLogger("saarthi.talkfirst")

GAP_S = int(os.getenv("TALKFIRST_GAP_S", "45"))        # first nudge after this much quiet, then 2x, 3x...
OWNER_GAP_S = int(os.getenv("TALKFIRST_OWNER_GAP_S", "60"))
MAX_NUDGES = int(os.getenv("TALKFIRST_MAX", "4"))
REOPEN_S = 120                                          # a page refresh within 2 min does not greet again
REAL_CALLS = os.getenv("REAL_CALLS", "0") == "1"        # demo: calls are simulated and labelled

_drv: dict[int, dict] = {}      # vehicle_id -> {"opened", "last_nudge", "n", "busy"}
_own: dict[int, dict] = {}      # fleet_id -> same
_offers: dict[str, tuple[float, str]] = {}   # "drv:<vid>" / "own:<fid>" -> (time, rewritten request on "haan")

GREET_OTHER = {"te-IN": "నమస్తే బాస్! సారథి సిద్ధంగా ఉన్నాడు. ఈరోజు ప్రయాణం ఎలా ఉంది?",
               "ta-IN": "வணக்கம் பாஸ்! சாரதி தயார். இன்றைய பயணம் எப்படி?",
               "kn-IN": "ನಮಸ್ಕಾರ ಬಾಸ್! ಸಾರಥಿ ಸಿದ್ಧ. ಇವತ್ತಿನ ಪ್ರಯಾಣ ಹೇಗಿದೆ?",
               "mr-IN": "राम राम बॉस! सारथी हजर आहे. आजचा प्रवास कसा चाललाय?",
               "gu-IN": "રામ રામ બોસ! સારથી હાજર છે. આજની સફર કેવી ચાલે છે?",
               "bn-IN": "নমস্কার বস! সারথি হাজির। আজকের যাত্রা কেমন চলছে?",
               "ml-IN": "നമസ്കാരം ബോസ്! സാരഥി റെഡി. ഇന്നത്തെ യാത്ര എങ്ങനെ?",
               "pa-IN": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ ਬੌਸ! ਸਾਰਥੀ ਹਾਜ਼ਰ ਹੈ। ਅੱਜ ਦਾ ਸਫ਼ਰ ਕਿਵੇਂ ਚੱਲ ਰਿਹਾ?",
               "od-IN": "ନମସ୍କାର ବସ୍! ସାରଥି ହାଜର। ଆଜିର ଯାତ୍ରା କେମିତି?"}


# ---------------- small helpers ----------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _first(name: str | None) -> str:
    return (name or "").replace("(demo)", "").strip().split(" ")[0] or "बॉस"


def _ji(name: str) -> str:
    return name if name == "बॉस" else f"{name} जी"


def _speed(v: dict) -> str:
    s = round(v.get("last_speed_kmph") or 0)
    return f"{s} km/h पे चल रही है" if s >= 3 else "रुकी हुई है"


LABEL_HI = {"Documents": "कागज़", "E-way bill": "ई-वे बिल", "Tolls": "टोल", "Diesel": "डीज़ल", "Driving hours": "ड्राइविंग घंटे",
            "Location": "लोकेशन", "Incidents": "दिक्कत", "Freight": "भाड़ा", "Unconfirmed entries": "अधूरी एंट्री"}


def _hours(v: dict) -> float:
    if not v.get("driving_since"):
        return 0.0
    try:
        return (_now() - datetime.fromisoformat(v["driving_since"])).total_seconds() / 3600
    except Exception:
        return 0.0


def _docs(vid: int) -> tuple[list[dict], list[dict]]:
    today = date.today().isoformat()
    in15 = (date.today() + timedelta(days=15)).isoformat()
    rows = db.q("SELECT doc_type, expiry_date FROM documents WHERE vehicle_id=? ORDER BY expiry_date", (vid,))
    return ([r for r in rows if r["expiry_date"] < today], [r for r in rows if today <= r["expiry_date"] <= in15])


def _open_tolls(trip: dict | None) -> list[dict]:
    if not trip:
        return []
    return db.q("SELECT plaza, expected_fee FROM toll_crossings WHERE trip_id=? AND status IN ('unreported','asked') "
                "ORDER BY id", (trip["id"],))


def near_city(lat: float | None, lon: float | None) -> str | None:
    """Nearest known city (Hindi name) from the built-in city table: instant, no network."""
    if lat is None or lon is None:
        return None
    from .geo import CITY
    best, bd = None, 1e9
    for _k, (hi, la, lo) in CITY.items():
        d = math.hypot(la - lat, (lo - lon) * math.cos(math.radians(lat)))
        if d < bd:
            best, bd = hi, d
    return best if bd < 1.5 else None      # ~150 km


async def _weather_line(vid: int) -> str:
    from .agents.driver import Turn, make_tools
    from .fastlane import _SKY_HI, _WARN_HI
    try:
        tools = {t.name: t.fn for t in make_tools(vid, Turn(""))}
        w = await asyncio.wait_for(tools["weather_now"](), 4)
    except Exception:
        return ""
    if not w or w.get("error") or w.get("temp_c") is None:
        return ""
    s = f"बाहर मौसम {_SKY_HI.get(w.get('now'), w.get('now'))}, {round(w['temp_c'])}°C"
    warns = [next((h for k, h in _WARN_HI.items() if x.startswith(k)), x) for x in w.get("warnings", [])]
    return s + ("। " + warns[0] if warns else "")


def _offer(key: str, request: str) -> None:
    _offers[key] = (time.time(), request)


def take_offer(key: str, text: str) -> str | None:
    """If the person just said yes to our last offer, return the request it stands for (else None)."""
    from .safety import yes_no
    hit = _offers.get(key)
    if not hit or time.time() - hit[0] > 300:
        return None
    yn = yes_no(text)
    if yn is None:
        return None
    _offers.pop(key, None)
    return hit[1] if yn else ""


def _say_driver(v: dict, text: str) -> None:
    db.log_agent("system", f"driver:{v['code']}", "message", {"text": text, "vehicle": v["code"], "proactive": True})


# ---------------- driver ----------------
async def driver_opener(v: dict) -> str:
    lang = v.get("lang") or "hi-IN"
    if lang not in ("hi-IN", "en-IN"):
        return GREET_OTHER.get(lang, GREET_OTHER["te-IN"])
    vid, name = v["id"], _first(v.get("driver_name"))
    t = db.active_trip(vid)
    parts = [f"राम राम {_ji(name)}! सारथी हाज़िर है।"]
    ask = None
    inc = db.one("SELECT description FROM incidents WHERE vehicle_id=? AND status='open' ORDER BY id DESC LIMIT 1", (vid,))
    if inc:
        parts.append(f"पिछली दिक्कत ({inc['description'][:60]}) अभी खुली है।")
        ask = "सब ठीक हो गया या मदद भेजूँ?"
    if t:
        where = near_city(v.get("last_lat"), v.get("last_lon"))
        parts.append(f"आज {t['origin']} से {t['destination']} का सफ़र है" + (f", अभी आप {where} के आसपास हैं।" if where else "।"))
    w = await _weather_line(vid)
    if w:
        parts.append(w + "।")
    h = _hours(v)
    if h >= 4.5 and not ask:
        parts.append(f"{h:.0f} घंटे से लगातार चला रहे हो।")
        ask = "अगले ढाबे पे 20 मिनट चाय हो जाए? हाँ बोलिए तो सबसे पास वाला ढूँढ दूँ।"
        _offer(f"drv:{vid}", "बहुत नींद आ रही है, पास में ढाबा बताओ")
    gone, soon = _docs(vid)
    if gone:
        parts.append(f"ध्यान दीजिए: {gone[0]['doc_type']} {gone[0]['expiry_date']} को ख़त्म हो चुका है, मालिक को बता दिया है।")
    elif soon:
        parts.append(f"{soon[0]['doc_type']} {soon[0]['expiry_date']} को ख़त्म होगा, याद रखिएगा।")
    tolls = _open_tolls(t)
    if tolls and not ask:
        ask = f"और {tolls[0]['plaza']} टोल का कितना लगा था? बस रकम बोल दीजिए, मैं लिख लूँगा।"
    if not t and not ask:
        ask = "गाड़ी अभी ख़ाली है — वापसी का लोड ढूँढ दूँ? हाँ बोलिए।"
        _offer(f"drv:{vid}", "गाड़ी खाली है, अगला लोड ढूंढो")
    parts.append(ask or "आज का सफ़र कैसा चल रहा है?")
    return " ".join(parts)


async def driver_nudge(v: dict, n: int) -> str | None:
    if (v.get("lang") or "hi-IN") not in ("hi-IN", "en-IN"):
        return None
    vid, name = v["id"], _first(v.get("driver_name"))
    t = db.active_trip(vid)
    topics = []
    tolls = _open_tolls(t)
    if tolls:
        topics.append(f"{_ji(name)}, एक बात रह गई — {tolls[0]['plaza']} टोल का कितना लगा था? रकम बोल दीजिए, मैं लिख लूँगा।")
    if _hours(v) >= 3:
        topics.append(f"{_hours(v):.0f} घंटे हो गए गाड़ी चलाते — थोड़ा पानी पी लीजिए। चाय ब्रेक चाहिए तो हाँ बोलिए, ढाबा ढूँढ दूँ।")
    w = await _weather_line(vid)
    if w:
        topics.append(f"मौसम अपडेट: {w.replace('बाहर मौसम ', 'अभी ')}। आगे रास्ते का हाल जानना हो तो पूछिए।")
    gone, soon = _docs(vid)
    if gone or soon:
        d = (gone or soon)[0]
        topics.append(f"याद दिला दूँ: {d['doc_type']} {d['expiry_date']} को {'ख़त्म हो चुका' if gone else 'ख़त्म होगा'}। "
                      f"अगले शहर में renew करवा लें?")
    if not t:
        topics.append("गाड़ी ख़ाली खड़ी है तो पैसा नहीं बन रहा — पास का लोड ढूँढ दूँ? हाँ बोलिए।")
    topics.append(f"{_ji(name)}, घर पे सब कैसे हैं? बच्चे ठीक? कोई पुराना गाना लगा दूँ — बस 'गाना बजाओ' बोलिए।")
    pick = topics[n % len(topics)]
    if "ढाबा ढूँढ" in pick:
        _offer(f"drv:{vid}", "बहुत नींद आ रही है, पास में ढाबा बताओ")
    elif "लोड ढूँढ" in pick:
        _offer(f"drv:{vid}", "गाड़ी खाली है, अगला लोड ढूंढो")
    return pick


def _last_activity(targets: list[str], froms: list[str]) -> float:
    ph, pf = ",".join("?" * len(targets)), ",".join("?" * len(froms))
    r = db.one(f"SELECT MAX(ts) ts FROM agent_log WHERE (kind='message' AND to_agent IN ({ph})) OR "
               f"(kind IN ('driver_said','owner_said') AND from_agent IN ({pf}))", (*targets, *froms))
    if not r or not r["ts"]:
        return 0.0
    try:
        return datetime.fromisoformat(r["ts"]).timestamp()
    except Exception:
        return 0.0


def on_driver_poll(v: dict, since: int) -> None:
    """Called on every driver-page poll (2.5 s). Schedules the greeting / a nudge; never blocks the poll."""
    if os.getenv("TALKFIRST", "1") != "1":
        return
    from .bus import bus
    if v["id"] in bus.live:                    # live voice call: companion.py's own talk-first loop runs
        return
    st = _drv.setdefault(v["id"], {"opened": 0.0, "last_nudge": 0.0, "n": 0, "busy": False})
    if st["busy"]:
        return
    now = time.time()
    if since == 0:
        if now - st["opened"] < REOPEN_S:
            return
        st.update(opened=now, n=0, busy=True)
        asyncio.create_task(_run(st, lambda: driver_opener(v), lambda txt: _say_driver(v, txt)))
        return
    code = v["code"]
    last = _last_activity([f"driver:{code}"], [f"driver:{code}"])
    said = db.one("SELECT MAX(ts) ts FROM agent_log WHERE kind='driver_said' AND from_agent=?", (f"driver:{code}",))
    if said and said["ts"] and datetime.fromisoformat(said["ts"]).timestamp() > st["last_nudge"]:
        st["n"] = 0                         # the driver talked after our last nudge: start the gaps over
    if st["n"] >= MAX_NUDGES or now - last < GAP_S * (1 + st["n"]):
        return
    n = st["n"]
    st.update(busy=True, last_nudge=now, n=n + 1)
    asyncio.create_task(_run(st, lambda: driver_nudge(db.vehicle(v["id"]), n), lambda txt: _say_driver(v, txt)))


async def _run(st: dict, make, say) -> None:
    try:
        txt = await make()
        if txt:
            say(txt)
    except Exception:
        log.exception("talk-first message failed")
    finally:
        st["busy"] = False


# ---------------- owner (CEO Agent) ----------------
def owner_opener(fleet: dict) -> str:
    from . import sentinel
    h = sentinel.fleet_health(fleet["id"])
    owner = _first(fleet.get("owner_name"))
    owner = "मालिक" if owner == "बॉस" else f"{owner} जी"
    n_bad = sum(1 for t in h["trucks"] if t["status"] == "bad")
    n_warn = sum(1 for t in h["trucks"] if t["status"] == "warn")
    state = (f"{n_bad} गाड़ी पर आपका ध्यान अभी चाहिए" if n_bad else f"सब चल रहा है, {n_warn} गाड़ी में छोटी बातें बाकी"
             if n_warn else f"सब ठीक — सभी {len(h['trucks'])} गाड़ियाँ बढ़िया चल रही हैं")
    parts = [f"नमस्ते {owner}! मैं आपका CEO सारथी — सब गाड़ियों पर नज़र रखे हुए हूँ।",
             f"अभी का हाल: {state} (चेकलिस्ट {h['score_pct']}% ठीक)।"]
    items = [(t["status"] == "bad", t["vehicle"], c) for t in h["trucks"] for c in t["checks"]
             if c["status"] != "ok" and c["label"] in LABEL_HI]
    items.sort(key=lambda x: (not x[0], x[2]["status"] != "bad"))
    if items:
        parts.append("सबसे ज़रूरी: " + "; ".join(f"{code} — {LABEL_HI[c['label']]}: {c['detail']}" for _b, code, c in items[:3]) + "।")
    idle = [t for t in h["trucks"] if not t["trip"]]
    if idle:
        parts.append(f"{idle[0]['vehicle']} ख़ाली खड़ी है। उसके लिए लोड ढूँढ के, कागज़ और पार्टी जाँच के बुक कर दूँ? हाँ बोलिए।")
        _offer(f"own:{fleet['id']}", "कोई गाड़ी खाली है तो उसके लिए लोड ढूंढो")
    else:
        parts.append("बताइए, आज क्या करना है — नई ट्रिप का प्लान, मुनाफ़ा, या किसी गाड़ी का हाल?")
    return " ".join(parts)


def owner_nudge(fleet: dict, n: int) -> str | None:
    from .fastlane import owner_papers
    topics = []
    due = db.one("SELECT COUNT(*) c, COALESCE(SUM(amount_due - paid),0) s FROM invoices WHERE fleet_id=? "
                 "AND status NOT IN ('paid','closed') AND COALESCE(amount_due,0) > COALESCE(paid,0)", (fleet["id"],))
    if due and due["c"]:
        from .agents.common import inr
        topics.append(f"पैसे की बात: {due['c']} बिल में {inr(due['s'])} बाकी है। किसका पेमेंट सबसे पुराना है, बताऊँ?")
    papers = owner_papers(fleet, True)
    if "नहीं हो रहे" not in papers:
        topics.append("कागज़ों की याद: " + papers + " renew का रिमाइंडर ड्राइवर को भेज दूँ?")
    tolls = db.one("SELECT COUNT(*) c FROM toll_crossings tc JOIN vehicles v ON v.id=tc.vehicle_id "
                   "WHERE v.fleet_id=? AND tc.status IN ('unreported','asked')", (fleet["id"],))
    if tolls and tolls["c"]:
        topics.append(f"{tolls['c']} टोल की रकम ड्राइवरों ने अभी नहीं बताई — मैं उनसे पूछ रहा हूँ। FASTag स्टेटमेंट से मिला दूँ?")
    for v in db.q("SELECT * FROM vehicles WHERE fleet_id=? ORDER BY code", (fleet["id"],)):
        t = db.active_trip(v["id"])
        if t:
            where = near_city(v.get("last_lat"), v.get("last_lon"))
            topics.append(f"{v['code']} ({t['origin']} → {t['destination']})" + (f" अभी {where} के पास है" if where else " चल रही है")
                          + f", {_speed(v)}। क्लाइंट को ट्रैकिंग लिंक भेज दूँ?")
            break
    topics.append("कोई नई ट्रिप सोच रहे हैं? बस शहर, वज़न और भाड़ा बोलिए — रूट, टोल, डीज़ल और मुनाफ़ा तुरंत बता दूँगा।")
    return topics[n % len(topics)]


def owner_poll(fleet: dict, opening: bool) -> str | None:
    """Office page asks every 20 s (and once on open). Returns what the CEO Agent says first, or None."""
    if os.getenv("TALKFIRST", "1") != "1":
        return None
    st = _own.setdefault(fleet["id"], {"opened": 0.0, "last_nudge": 0.0, "n": 0, "busy": False, "last_chat": 0.0})
    now = time.time()
    if opening:
        st.update(opened=now, n=0, last_nudge=now)
        txt = owner_opener(fleet)
    else:
        quiet = now - max(st["last_nudge"], st["last_chat"], st["opened"])
        if st["n"] >= MAX_NUDGES - 1 or quiet < OWNER_GAP_S * (1 + st["n"]):
            return None
        txt = owner_nudge(fleet, st["n"])
        st.update(n=st["n"] + 1, last_nudge=now)
    if txt:
        db.log_agent(f"ceo:{fleet['code']}", f"owner:{fleet['code']}", "message", {"text": txt, "proactive": True})
    return txt


def owner_talked(fleet: dict) -> None:
    st = _own.setdefault(fleet["id"], {"opened": 0.0, "last_nudge": 0.0, "n": 0, "busy": False, "last_chat": 0.0})
    st.update(last_chat=time.time(), n=0)


# ---------------- simulated calls ----------------
_CALL = re.compile(r"(call|कॉल|काल|फ़ोन|फोन|phone|baat karao|बात कराओ|बात करवाओ|dial|मिलाओ|lagao|लगाओ)", re.I)
_WHO_OWNER = re.compile(r"(malik|maalik|मालिक|owner|seth|सेठ)", re.I)
_WHO_HOME = re.compile(r"(ghar|घर|wife|biwi|बीवी|पत्नी|mummy|मम्मी|maa\b|माँ|bete|बेटे|family)", re.I)
_WHO_MECH = re.compile(r"(mechanic|मैकेनिक|mistri|मिस्त्री|garage|गैराज)", re.I)
_WHO_DRIVER = re.compile(r"(driver|ड्राइवर|chalak|चालक)", re.I)
NOTE = "(डेमो कॉल — असली फ़ोन कॉल के लिए WhatsApp Business / टेलीफ़ोनी key लगती है)"


def simulated_call_line(name: str, phone: str | None = None) -> str:
    who = f"{name} {phone}" if phone else name
    return (f"📞 {who} को कॉल लगाया {NOTE}। {name}: \"हाँ जी, लोकेशन भेज दीजिए, 20-25 मिनट में पहुँचता हूँ।\" (डेमो)")


_RELAY = re.compile(r"(malik|maalik|मालिक|owner|seth|सेठ|office|ऑफिस|ऑफ़िस|दफ्तर)\s*(ji|जी)?\s*(ko|को|se|से|tak|तक)\s+"
                    r"(.*?)\s*(bolo|bol do|bol dena|batao|bata do|bata dena|kaho|keh do|kah do|bhejo|बोलो|बोल दो|बोल देना|"
                    r"बताओ|बता दो|बता देना|कहो|कह दो|कहना|भेजो)(.*)", re.I | re.S)
_RELAY2 = re.compile(r"(malik|maalik|मालिक|owner|seth|सेठ|office|ऑफिस|ऑफ़िस)\s*(ji|जी)?\s*(ko|को)\s+"
                     r"(bolo|bol do|batao|bata do|kaho|keh do|बोलो|बोल दो|बताओ|बता दो|कहो|कह दो)\s*(ki|कि)?\s*(.+)", re.I | re.S)
_INCIDENT = re.compile(r"(टायर|tyre|tire|पंक्चर|puncture|kharab|ख़राब|खराब|accident|एक्सीडेंट|दुर्घटना|आग|fire|"
                       r"chori|चोरी|लूट|police|पुलिस|emergency|इमरजेंसी)", re.I)


async def driver_relay(v: dict, text: str) -> str | None:
    """'Malik ko bolo …' — the Driver Agent passes the message to the CEO Agent / owner at once (no AI needed).
    Breakdowns and emergencies are left to their own flow, which alerts the owner with location and help."""
    if _CALL.search(text) or _INCIDENT.search(text):
        return None
    m2 = _RELAY2.search(text)
    m = None if m2 else _RELAY.search(text)
    if not (m or m2):
        return None
    msg = (m2.group(6) if m2 else (m.group(4) + " " + m.group(6))).strip(" ,.।-") or text.strip()
    from .agents import ceo
    from .bus import bus
    f = db.fleet(v["fleet_id"])
    name = v.get("driver_name") or "ड्राइवर"
    db.log_agent(f"driver:{v['code']}", f"ceo:{f['code']}", "driver_message", {"message": msg})
    o = ceo.owner_out(f, f"📩 {name} ({v['reg_label']}): {msg}")
    if o:
        await bus.send(o)
    return f"✅ मालिक को भेज दिया: \"{msg}\"। जवाब आते ही आपको बता दूँगा।"


async def driver_call(v: dict, text: str) -> str | None:
    """Driver asks to call someone: simulated, labelled call (no real dialling in the demo)."""
    if REAL_CALLS or not _CALL.search(text):
        return None
    name = _first(v.get("driver_name"))
    if _WHO_OWNER.search(text):
        f = db.fleet(v["fleet_id"])
        db.log_agent(f"driver:{v['code']}", f"owner:{f['code']}", "message",
                     {"text": f"📞 {v['code']} के ड्राइवर {name} आपसे बात करना चाहते हैं {NOTE}", "proactive": True})
        return (f"📞 मालिक को कॉल लगाया {NOTE}। मालिक: \"हाँ {name}, बोलो, सब ठीक? जो चाहिए सारथी को बता दो, "
                f"मुझे सब पहुँच जाता है।\" (डेमो)")
    if _WHO_HOME.search(text):
        return (f"📞 घर पे कॉल लगाया {NOTE}। गाड़ी चलते हुए लंबी बात मत करना — मैं बता देता हूँ कि आप ठीक हो और "
                f"अगले ढाबे पे रुक के बात करोगे।")
    if _WHO_MECH.search(text):
        from .agents.driver import Turn, make_tools
        try:
            tools = {t.name: t.fn for t in make_tools(v["id"], Turn(text))}
            r = await asyncio.wait_for(tools["find_help"](kind="mechanic"), 8)
            res = r.get("results") or []
        except Exception:
            res = []
        if res:
            return f"सबसे पास {res[0]['name']}, {res[0]['km']} km। " + simulated_call_line(res[0]["name"], res[0].get("phone"))
        return "📞 " + NOTE + " — पास का मैकेनिक ढूँढ रहा हूँ, 'टायर फट गया' जैसा बताइए तो मालिक को भी ख़बर कर दूँगा।"
    return None


def owner_call(fleet: dict, text: str) -> str | None:
    """Owner asks to call a driver: simulated call, the 'answer' is built from the truck's live data."""
    if REAL_CALLS or not _CALL.search(text):
        return None
    from .fastlane import _VEH
    m = _VEH.search(text)
    v = None
    if m:
        v = db.one("SELECT * FROM vehicles WHERE fleet_id=? AND UPPER(code)=?", (fleet["id"], m.group(1).upper()))
    elif _WHO_DRIVER.search(text):
        v = db.one("SELECT * FROM vehicles WHERE fleet_id=? AND last_seen IS NOT NULL ORDER BY last_seen DESC LIMIT 1",
                   (fleet["id"],))
    if not v:
        return None
    t = db.active_trip(v["id"])
    name = _first(v.get("driver_name"))
    name = "ड्राइवर" if name == "बॉस" else name
    where = near_city(v.get("last_lat"), v.get("last_lon"))
    if t:
        ans = (f"मालिक, राम राम! {t['destination']} की तरफ़ जा रहा हूँ" + (f", अभी {where} के पास" if where else "")
               + (f", {round(v.get('last_speed_kmph') or 0)} km/h पे।" if (v.get("last_speed_kmph") or 0) >= 3
                  else ", अभी चाय के लिए रुका हूँ।") + " सब ठीक है।")
    else:
        ans = "मालिक, राम राम! गाड़ी अभी ख़ाली है, अगला लोड मिलते ही निकल जाऊँगा।"
    db.log_agent(f"ceo:{fleet['code']}", f"driver:{v['code']}", "message",
                 {"text": f"📞 मालिक का कॉल आया {NOTE}", "vehicle": v["code"], "proactive": True})
    who = f"{v['code']} के ड्राइवर" + ("" if name == "ड्राइवर" else f" {name}")
    return (f"📞 {who} को कॉल लगाया {NOTE}।\n{name} (डेमो जवाब, ट्रक के लाइव डेटा से): \"{ans}\"")
