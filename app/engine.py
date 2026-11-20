"""Agentic ecosystem: an event bus plus playbooks where Saarthi's agents coordinate to resolve chaos.
Calls, bookings and messages to third parties are SIMULATED for the demo and labelled as such."""
from __future__ import annotations

import asyncio
import itertools
import json
from datetime import date, datetime, timedelta

from . import gemini
from .fleet import AGENTS, CITIES, DIESEL_RS_PER_L, KMPL, TOLL_RS_PER_KM, Fleet, haversine, road_km
from .predict import predict_trip

_ids = itertools.count(1)


class Bus:
    def __init__(self):
        self.events: list[dict] = []
        self.subs: set[asyncio.Queue] = set()

    def publish(self, kind: str, payload: dict):
        msg = {"type": kind, **payload}
        if kind == "event":
            self.events.append(payload)
            self.events = self.events[-300:]
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def subscribe(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subs.add(q)
        return q


class Engine:
    def __init__(self, fleet: Fleet, bus: Bus):
        self.fleet, self.bus = fleet, bus
        self.pace = 1.0
        self.running: set[str] = set()

    # ------------------------------------------------------------ primitives
    async def say(self, agent: str, text: str, kind: str = "step", truck: str | None = None, wait: float = 1.1,
                  **data):
        ev = {"id": next(_ids), "ts": datetime.now().strftime("%H:%M:%S"), "agent": agent,
              "agent_name": AGENTS[agent]["name"], "color": AGENTS[agent]["color"], "kind": kind,
              "text": text, "truck": truck, "data": data or None}
        self.bus.publish("event", ev)
        if wait:
            await asyncio.sleep(wait * self.pace)
        return ev

    async def call(self, agent: str, who: str, lines: list[tuple[str, str]], truck=None):
        await self.say(agent, f"Calling {who} (simulated call)", kind="call", truck=truck, wait=0.8)
        for speaker, line in lines:
            await self.say(agent, f"{speaker}: {line}", kind="call-line", truck=truck, wait=1.0)

    async def dash(self, truck: str, hindi: str, mood: str = "talking"):
        await self.say("dash", hindi, kind="dash", truck=truck, wait=1.2, mood=mood)

    def push_state(self):
        self.bus.publish("state", self.fleet.snapshot())

    async def narrate(self, playbook: str, facts: dict, fallback: str):
        txt = await gemini.generate(
            "Write ONE short sentence (max 30 words) for a busy Indian truck-fleet owner summarising what Saarthi's "
            f"agents just did and the money impact. Simple English with a little Hinglish is fine. Playbook: {playbook}. "
            f"Facts: {json.dumps(facts, default=str)}",
            system="You are Saarthi, an AI fleet co-pilot. Be concrete, use rupee figures from the facts only.",
            timeout=7)
        return txt or fallback

    async def resolve(self, playbook: str, title: str, impact_rs: int, facts: dict, fallback: str):
        summary = await self.narrate(playbook, facts, fallback)
        self.fleet.stats["incidents_resolved"] += 1
        await self.say("core", summary, kind="resolved", wait=0.2, title=title, impact_rs=impact_rs,
                       playbook=playbook, ai=gemini.ready())
        self.push_state()

    def find(self, tid=None, status=None):
        if tid and tid in self.fleet.trucks:
            return self.fleet.trucks[tid]
        for t in self.fleet.trucks.values():
            if status and t["status"] in status:
                return t
        return None

    def order_of(self, t):
        return self.fleet.orders.get(t["order"]) if t and t.get("order") else None

    async def run(self, name: str, target: str | None = None):
        fn = PLAYBOOKS.get(name)
        if not fn:
            return {"ok": False, "error": f"unknown playbook {name}"}
        if name in self.running:
            return {"ok": False, "error": "already running"}
        self.running.add(name)

        async def wrapped():
            try:
                await fn(self, target)
            except Exception as e:  # noqa: BLE001
                await self.say("core", f"Playbook {name} hit an error: {e}", kind="alert", wait=0)
            finally:
                self.running.discard(name)
                self.push_state()

        asyncio.create_task(wrapped())
        return {"ok": True, "started": name}


# ====================================================================== playbooks
async def pb_dispatch(e: Engine, target=None):
    f = e.fleet
    f.next_order += 1
    origin, dest = "Chittorgarh", "Ahmedabad"
    oid = f"ORD-{f.next_order}"
    await e.say("core", f"New order {oid}: Mewar Cement Ltd, 25 t cement bags, {origin} -> {dest}, deliver by tomorrow 14:00.",
                kind="alert")
    cands = [t for t in f.trucks.values() if t["status"] in ("idle", "empty")]
    cands.sort(key=lambda t: haversine((t["lat"], t["lng"]), CITIES[origin]))
    await e.say("dispatch", f"Scanning {len(f.trucks)} trucks: {len(cands)} free, checking distance, driver hours, documents and health.")
    best = None
    for t in cands:
        bad = [d for d in f.docs_report(0) if d["truck"] == t["id"]]
        if not bad and t["drive_hours"] < 9:
            best = t
            break
    best = best or cands[0]
    dist = round(haversine((best["lat"], best["lng"]), CITIES[origin]) * 1.22)
    await e.say("dispatch", f"Best fit: {best['id']} ({best['driver']}), {dist} km from pickup, health {best['health']}%, all documents valid.",
                truck=best["id"])
    p = await predict_trip(origin, dest, tons=25)
    w = p["weather"][1]
    await e.say("route", f"Predicted: {p['km']} km, ETA {p['eta_hours']} h (arrive {p['arrive']}). Weather on corridor: {w['condition']}, "
                f"{w['temp_c']} C, rain chance {w['rain_chance_12h']}%. Best departure {p['best_departure']['depart']}.",
                truck=best["id"], route=p["coords"])
    await e.say("route", f"Trip cost forecast: diesel Rs {p['cost']['diesel']:,}, tolls Rs {p['cost']['toll']:,}, driver Rs {p['cost']['driver']:,}. "
                + " ".join(p["advice"][:2]), truck=best["id"])
    freight = 58000
    f.orders[oid] = {"id": oid, "truck": best["id"], "from": origin, "to": dest, "km": p["km"], "cargo": "Cement bags",
                     "tons": 25, "shipper": "Mewar Cement Ltd", "consignee": "Gujarat BuildMart", "freight": freight,
                     "status": "assigned", "progress": 0.0, "eta_h": p["eta_hours"]}
    best.update(status="loading", order=oid, city=origin, lat=CITIES[origin][0], lng=CITIES[origin][1])
    e.push_state()
    await e.say("dispatch", f"{oid} assigned to {best['id']}. Status: ASSIGNED -> LOADING.", truck=best["id"])
    await e.dash(best["id"], f"{best['driver'].split()[0]} ji, naya trip! Chittorgarh se Ahmedabad, 25 ton cement. "
                 f"Loading Mewar Cement gate 2 par. Nikalne ka sabse accha samay {p['best_departure']['depart']}.")
    await e.say("customer", "Live tracking link sent to Gujarat BuildMart and Mewar Cement on WhatsApp (simulated).")
    f.orders[oid]["status"] = "on_the_way"
    best["status"] = "on_the_way"
    e.push_state()
    await e.say("dispatch", f"Loading complete. {oid} is ON THE WAY.", truck=best["id"])
    margin = freight - p["cost"]["total"]
    await e.resolve("dispatch", f"{oid} dispatched", margin,
                    {"order": oid, "truck": best["id"], "eta_h": p["eta_hours"], "freight": freight,
                     "trip_cost": p["cost"]["total"], "margin": margin},
                    f"{oid} dispatched on {best['id']} in under a minute; predicted margin Rs {margin:,}.")


async def pb_breakdown(e: Engine, target=None):
    f = e.fleet
    t = e.find(target) or e.find("RJ09GA4501")
    o = e.order_of(t)
    t["status"] = "breakdown"
    e.push_state()
    near = f.nearest_city(t["lat"], t["lng"])
    await e.say("dash", f"{t['driver']} on {t['id']}: \"Engine garam ho raha hai, coolant leak ho raha hai.\" Truck stopped near {near}.",
                kind="alert", truck=t["id"])
    diag = await gemini.generate(
        "Truck: Tata multi-axle, symptoms: engine overheating, coolant leak, 28 t loaded, highway. "
        "Give the most likely cause and repair time in ONE short line.", timeout=6)
    await e.say("recovery", "Diagnosis: " + (diag or "likely radiator hose / coolant pipe failure. Repair 2-3 h roadside."),
                truck=t["id"])
    mech = min(f.mechanics, key=lambda m: haversine((t["lat"], t["lng"]), (m["lat"], m["lng"])))
    md = round(haversine((t["lat"], t["lng"]), (mech["lat"], mech["lng"])) * 1.25)
    await e.call("recovery", mech["name"], [("Saarthi", f"Truck {t['id']} stuck near {near}, coolant leak. Can you come?"),
                                           (mech["name"], f"Haan, mechanic bhej rahe hain. {max(20, md * 2)} minute mein pahunchega.")],
                 truck=t["id"])
    await e.say("recovery", f"Mechanic from {mech['name']} ({mech['city']}, rating {mech['rating']}) dispatched, {md} km away.",
                truck=t["id"], mechanic=mech)
    await e.dash(t["id"], f"{t['driver'].split()[0]} ji, ghabraiye mat. Mechanic {max(20, md * 2)} minute mein aa raha hai. "
                 "Hazard lights on rakhiye aur truck ke peeche triangle lagaiye.", mood="alert")
    relief = None
    if o:
        await e.say("recovery", f"Repair 2-3 h would miss the delivery window for {o['consignee']}. Looking for a relief truck nearby.")
        frees = [x for x in f.trucks.values() if x["status"] in ("empty", "idle") and x["id"] != t["id"]]
        if frees:
            relief = min(frees, key=lambda x: haversine((x["lat"], x["lng"]), (t["lat"], t["lng"])))
            rd = round(haversine((relief["lat"], relief["lng"]), (t["lat"], t["lng"])) * 1.22)
            await e.say("freight", f"Relief truck {relief['id']} is EMPTY at {relief.get('city') or 'nearby'}, {rd} km away. "
                        "Assigning it for cargo transfer - empty kilometres become paid kilometres.", truck=relief["id"])
            relief.update(status="on_the_way", order=o["id"])
            o["truck"] = relief["id"]
            e.push_state()
            await e.dash(relief["id"], f"{relief['driver'].split()[0]} ji, emergency pickup! {near} ke paas ek truck khada hai, "
                         "maal transfer karke Ahmedabad le jaana hai.")
        new_eta = (datetime.now() + timedelta(hours=o["eta_h"] + 1.5)).strftime("%H:%M")
        await e.call("customer", o["consignee"], [("Saarthi", f"Your {o['cargo']} shipment is delayed by about 1.5 h due to a breakdown. New ETA {new_eta}."),
                                                  (o["consignee"], "Theek hai, update ke liye shukriya. Dock 4 ready rahega.")])
    t["status"] = "repair"
    e.push_state()
    penalty = 12000
    await e.resolve("breakdown", f"Breakdown recovered - {t['id']}", penalty,
                    {"truck": t["id"], "mechanic": mech["name"], "relief_truck": relief and relief["id"],
                     "sla_penalty_avoided": penalty},
                    f"{t['id']} breakdown handled: mechanic on the way, cargo moved to {relief['id'] if relief else 'relief truck'}, Rs {penalty:,} SLA penalty avoided.")


async def pb_empty(e: Engine, target=None):
    f = e.fleet
    t = e.find(target) if target else None
    t = t or e.find(status=("empty",)) or e.find(status=("idle",))
    city = t.get("city") or f.nearest_city(t["lat"], t["lng"])
    home = t["home"]
    back = road_km(city, home) if city != home else 250
    empty_cost = round(back / KMPL * DIESEL_RS_PER_L + back * TOLL_RS_PER_KM)
    await e.say("freight", f"{t['id']} is empty at {city}. Returning to {home} empty = {back} km, about Rs {empty_cost:,} lost in diesel and tolls.",
                kind="alert", truck=t["id"])
    loads = [ld for ld in f.loads if ld["from"] == city or road_km(ld["from"], city) < 150]
    if not loads:
        loads = sorted(f.loads, key=lambda ld: road_km(ld["from"], city))[:3]
    scored = []
    for ld in loads:
        dead = road_km(city, ld["from"]) if ld["from"] != city else 0
        km = road_km(ld["from"], ld["to"])
        home_gap = road_km(ld["to"], home) if ld["to"] != home else 0
        cost = (dead + km) / KMPL * DIESEL_RS_PER_L + (dead + km) * TOLL_RS_PER_KM
        score = ld["rate"] - cost - home_gap * 30
        scored.append((score, ld, km, dead))
    scored.sort(key=lambda s: -s[0])
    await e.say("freight", f"Searched the load board: {len(scored)} matching loads near {city}. Ranking by net earning and distance to home.")
    top = " | ".join(f"{s[1]['id']} {s[1]['from']}->{s[1]['to']} Rs {s[1]['rate']:,} (Rs {round(s[1]['rate']/max(s[2],1))}/km)"
                     for s in scored[:3])
    await e.say("freight", "Top options: " + top)
    best = scored[0][1]
    await e.call("freight", f"Shipper for {best['id']}", [("Saarthi", f"We have a 25 t truck at {city} now for your {best['cargo']} to {best['to']}. Rate Rs {best['rate']:,}?"),
                                                          ("Shipper", "Done. Loading slot in 1 hour, gate 1.")], truck=t["id"])
    km = road_km(best["from"], best["to"])
    f.next_order += 1
    oid = f"ORD-{f.next_order}"
    f.orders[oid] = {"id": oid, "truck": t["id"], "from": best["from"], "to": best["to"], "km": km, "cargo": best["cargo"],
                     "tons": best["tons"], "shipper": "Load board partner", "consignee": f"{best['to']} buyer",
                     "freight": best["rate"], "status": "loading", "progress": 0.0, "eta_h": round(km / 45, 1)}
    t.update(status="loading", order=oid)
    f.loads = [x for x in f.loads if x["id"] != best["id"]]
    f.stats["revenue_recovered"] += best["rate"]
    f.stats["empty_km_avoided"] += int(km)
    e.push_state()
    await e.say("freight", f"Booked {best['id']} as {oid}: {best['cargo']}, {best['from']} -> {best['to']}, Rs {best['rate']:,} (simulated booking).",
                truck=t["id"])
    await e.dash(t["id"], f"{t['driver'].split()[0]} ji, khaali wapas nahi jaana! {best['to']} ka maal mil gaya, "
                 f"Rs {best['rate']:,} ka. Gate 1 par ek ghante mein loading.")
    await e.resolve("empty_return", f"Empty return recovered - {t['id']}", best["rate"],
                    {"truck": t["id"], "load": best["id"], "revenue": best["rate"], "empty_km_avoided": km,
                     "empty_cost_avoided": empty_cost},
                    f"Empty truck {t['id']} now earns Rs {best['rate']:,} on the way home; {int(km)} empty km avoided.")


async def pb_refused(e: Engine, target=None):
    f = e.fleet
    o = f.orders.get(target) if target else None
    o = o or next((x for x in f.orders.values() if x["status"] == "on_the_way" and x["progress"] > 0.6), None) \
        or next(x for x in f.orders.values() if x["status"] == "on_the_way")
    t = f.trucks[o["truck"]]
    await e.say("dash", f"{t['driver']} at {o['to']}: consignee {o['consignee']} REFUSED delivery of {o['id']} ({o['cargo']}).",
                kind="alert", truck=t["id"])
    await e.call("customer", o["consignee"], [("Saarthi", f"Why is {o['id']} being refused?"),
                                              (o["consignee"], "Site abhi ready nahi hai, unloading labour nahi hai. Kal subah le aao.")])
    await e.call("customer", o["shipper"], [("Saarthi", f"Your consignee refused {o['id']} - site not ready. They ask for redelivery tomorrow."),
                                            (o["shipper"], "Maal wapas mat lao. Jo sasta ho woh karo, charges consignee ko bill karna.")])
    back = road_km(o["to"], o["from"])
    ret_cost = round(back / KMPL * DIESEL_RS_PER_L + back * TOLL_RS_PER_KM)
    wait_cost = 600 * 16 + 900
    store_cost = 2500 + 1200
    await e.say("core", f"Options: A) wait at site till tomorrow Rs {wait_cost:,}  B) return to {o['from']} Rs {ret_cost:,}  "
                f"C) store at partner warehouse 12 km away + redeliver Rs {store_cost:,}.")
    await e.say("core", f"Choosing C. Truck freed for the next load tonight; charges billed to {o['consignee']} as per contract.")
    await e.dash(t["id"], f"{t['driver'].split()[0]} ji, maal partner godown mein utarna hai, 12 km aage. "
                 "Location bhej di hai. Wahan se aap free ho, agla load ready hai.")
    await e.say("customer", f"Shipper, consignee and driver all confirmed. Redelivery booked tomorrow 10:00 (simulated).")
    saved = ret_cost - store_cost
    await e.resolve("delivery_refused", f"Refused delivery handled - {o['id']}", saved,
                    {"order": o["id"], "chosen": "warehouse storage + redelivery", "cost": store_cost,
                     "return_cost_avoided": ret_cost, "saved": saved},
                    f"Refused delivery of {o['id']} handled in minutes: storage + redelivery for Rs {store_cost:,} instead of Rs {ret_cost:,} return.")


async def pb_docs(e: Engine, target=None):
    f = e.fleet
    rep = f.docs_report(15)
    await e.say("compliance", f"Daily document scan of {len(f.trucks)} trucks: {len(rep)} documents expire within 15 days.",
                kind="alert", docs=rep)
    actions = {"Insurance": "Requested renewal quotes from 3 insurers; best premium selected",
               "PUC": "Booked PUC test slot at the nearest authorised centre",
               "Fitness": "Booked RTO fitness inspection appointment",
               "National Permit": "Filed permit renewal on the transport portal"}
    for d in rep:
        urgent = " - DISPATCH BLOCKED until renewed" if d["days_left"] <= 3 else ""
        await e.say("compliance", f"{d['truck']} {d['doc']} expires in {d['days_left']} days ({d['expires']}). "
                    f"{actions.get(d['doc'], 'Renewal started')} (simulated){urgent}.", truck=d["truck"])
        f.tasks.append({"type": "renewal", "truck": d["truck"], "doc": d["doc"], "status": "in progress"})
    e.push_state()
    for d in rep:
        new = (date.today() + timedelta(days=365 if d["doc"] != "PUC" else 180)).isoformat()
        f.trucks[d["truck"]]["docs"][d["doc"]] = new
        await e.say("compliance", f"Renewed {d['doc']} for {d['truck']} uploaded and verified - new expiry {new}.",
                    truck=d["truck"], wait=0.8)
    for task in f.tasks:
        if task["type"] == "renewal":
            task["status"] = "verified"
    fine = 5000 * len(rep)
    await e.resolve("docs_expiring", "All expiring documents renewed", fine,
                    {"documents": len(rep), "fines_avoided_estimate": fine},
                    f"{len(rep)} expiring documents renewed and verified; challans and dispatch blocks avoided.")


async def pb_cancel(e: Engine, target=None):
    f = e.fleet
    o = f.orders.get(target) if target else None
    o = o or next((x for x in f.orders.values() if x["status"] == "on_the_way" and x["progress"] < 0.4), None) \
        or next(x for x in f.orders.values() if x["status"] == "on_the_way")
    t = f.trucks[o["truck"]]
    done = round(o["km"] * o["progress"])
    await e.say("customer", f"{o['shipper']} CANCELLED {o['id']} after dispatch. {t['id']} has already driven {done} km.",
                kind="alert", truck=t["id"])
    diesel = round(done / KMPL * DIESEL_RS_PER_L)
    toll = round(done * TOLL_RS_PER_KM)
    drv = 900
    exposure = diesel + toll + drv
    fee = round(o["freight"] * 0.15)
    await e.say("core", f"Exposure: diesel Rs {diesel:,} + tolls Rs {toll:,} + driver Rs {drv:,} = Rs {exposure:,}. "
                f"Contract allows a 15% cancellation fee: Rs {fee:,} billable to {o['shipper']}.")
    city = f.nearest_city(t["lat"], t["lng"])
    loads = sorted(f.loads, key=lambda ld: road_km(ld["from"], city) if ld["from"] != city else 0)
    alt = loads[0]
    dead = road_km(city, alt["from"]) if alt["from"] != city else 0
    await e.say("freight", f"Searching alternative freight near {city}: best is {alt['id']} {alt['from']} -> {alt['to']}, Rs {alt['rate']:,}, {dead} km away.",
                truck=t["id"])
    o["status"] = "cancelled"
    f.next_order += 1
    oid = f"ORD-{f.next_order}"
    km = road_km(alt["from"], alt["to"])
    f.orders[oid] = {"id": oid, "truck": t["id"], "from": alt["from"], "to": alt["to"], "km": km, "cargo": alt["cargo"],
                     "tons": alt["tons"], "shipper": "Load board partner", "consignee": f"{alt['to']} buyer",
                     "freight": alt["rate"], "status": "assigned", "progress": 0.0, "eta_h": round(km / 45, 1)}
    t.update(order=oid, status="on_the_way")
    f.loads = [x for x in f.loads if x["id"] != alt["id"]]
    e.push_state()
    await e.dash(t["id"], f"{t['driver'].split()[0]} ji, purana order cancel ho gaya, par chinta nahi. Naya maal {alt['from']} se "
                 f"{alt['to']} ka mil gaya hai. Route update kar diya.")
    await e.say("customer", f"Cancellation invoice of Rs {fee:,} raised to {o['shipper']} with GPS trip proof (simulated).")
    net = fee + alt["rate"] - exposure
    f.stats["loss_prevented"] += exposure
    await e.resolve("cancel_after_dispatch", f"Cancellation recovered - {o['id']}", net,
                    {"cancelled": o["id"], "exposure": exposure, "fee": fee, "new_load": alt["id"], "new_revenue": alt["rate"]},
                    f"Cancelled {o['id']}: Rs {exposure:,} exposure covered by Rs {fee:,} fee plus new load worth Rs {alt['rate']:,}.")


async def pb_detention(e: Engine, target=None):
    f = e.fleet
    t = e.find(target) or e.find(status=("waiting",)) or e.find(status=("loading",))
    wh_name, wh = next(iter(f.warehouses.items()))
    hours = t.get("wait_since_h", 8.2)
    t["status"] = "waiting"
    billable = round(hours - wh["free_hours"], 1)
    await e.say("detention", f"{t['id']} has been waiting {hours} h at {wh_name} (geo-fenced gate-in 06:10). Free time is {wh['free_hours']} h.",
                kind="alert", truck=t["id"])
    await e.say("detention", f"Detention clock: {billable} billable h x Rs {wh['detention_rate']} = Rs {round(billable * wh['detention_rate']):,} and rising.")
    await e.call("detention", wh["contact"], [("Saarthi", f"Truck {t['id']} has waited {hours} hours. When will loading start?"),
                                              (wh["contact"], "Dock full hai bhai, shaam tak dekhte hain.")], truck=t["id"])
    await e.say("detention", "No firm slot. Escalating to the shipper's logistics manager with gate-in GPS proof and the detention clause (simulated email + WhatsApp).",
                kind="alert")
    await e.call("detention", "Shipper logistics manager", [("Shipper", "Sorry for the delay. I've told the DC to load you at dock 3 at 15:30 sharp.")])
    charge = round((billable + 1.5) * wh["detention_rate"])
    f.stats["detention_billed"] += charge
    f.tasks.append({"type": "invoice", "truck": t["id"], "doc": f"Detention Rs {charge:,}", "status": "added to invoice"})
    t["status"] = "loading"
    e.push_state()
    await e.dash(t["id"], f"{t['driver'].split()[0]} ji, 3:30 baje dock 3 par loading pakki ho gayi. Tab tak aaram kijiye, "
                 "canteen gate ke right side hai.")
    await e.resolve("detention", f"Detention escalated - {t['id']}", charge,
                    {"truck": t["id"], "hours_waited": hours, "detention_billed": charge, "slot": "15:30 dock 3"},
                    f"{t['id']} gets a dock at 15:30 after escalation; Rs {charge:,} detention added to the invoice.")


async def pb_fatigue(e: Engine, target=None):
    t = e.find(target) or e.find("RJ09GA4508")
    t["drive_hours"] = 9.3
    await e.say("dash", f"{t['id']}: {t['driver']} has driven 9.3 h today, blink rate and lane drift show fatigue (simulated camera signal).",
                kind="alert", truck=t["id"])
    await e.dash(t["id"], f"{t['driver'].split()[0]} ji, aap 9 ghante se chala rahe ho. 6 km aage Sher-e-Punjab dhaba hai. "
                 "Chaai aur 20 minute aaram, phir aage chalte hain. Main ETA update kar deta hoon.", mood="sleepy")
    o = e.order_of(t)
    await e.say("route", f"ETA updated +30 min for {o['id'] if o else 'current trip'}; consignee notified automatically.")
    await e.resolve("fatigue", f"Fatigue stop - {t['id']}", 0, {"truck": t["id"], "hours": 9.3},
                    f"Saarthi Dash stopped a tired driver on {t['id']} for a 20-minute rest before risk turned into an accident.")


async def pb_fastag(e: Engine, target=None):
    """Merged from Saarthi (Gnani build): FASTag deductions vs real NHAI plazas on the GPS road route."""
    t = e.find(target, ("on_the_way",)) if target else None
    t = t or next((x for x in e.fleet.trucks.values() if x["status"] == "on_the_way"
                   and (e.order_of(x) or {}).get("km", 0) > 250), None) or e.find(status=("on_the_way",))
    o = e.order_of(t)
    await e.say("fastag", f"{t['id']}: reading today's FASTag debit SMS for {o['from']} → {o['to']} (simulated bank SMS feed).",
                truck=t["id"])
    p = await predict_trip(o["from"], o["to"])
    e.bus.publish("route", {"route": p})
    plz =[x for x in p["tolls"]["plazas"] if x["fee_2019_20"]][:6]
    if len(plz) < 2:
        plz = [{"name": "Plaza A (estimated)", "fee_2019_20": 420}, {"name": "Plaza B (estimated)", "fee_2019_20": 380}]
    up = lambda f: round(f * 1.3 / 5) * 5
    await e.say("fastag", f"Matched against {p['tolls'].get('count', len(plz))} NHAI toll plazas on the road route "
                f"(government TIS data): " + ", ".join(x["name"] for x in plz[:4]) + ".", truck=t["id"])
    dup = plz[1]
    charge = up(dup["fee_2019_20"])
    await e.say("fastag", f"⚠ {dup['name']} charged twice within 4 minutes (Rs {charge:,} × 2). GPS shows ONE crossing.",
                kind="alert", truck=t["id"])
    await e.say("fastag", f"Dispute drafted for the issuer bank / NHAI 1033 with GPS proof: crossing time, "
                f"plaza and vehicle {t['id']} (sent as draft for owner approval).")
    e.fleet.stats["loss_prevented"] += charge
    e.fleet.stats["revenue_recovered"] += charge
    await e.resolve("fastag_audit", f"FASTag double charge caught - {t['id']}", charge,
                    {"truck": t["id"], "plaza": dup["name"], "refund_claim_rs": charge, "plazas_checked": len(plz)},
                    f"FASTag Auditor caught a double toll at {dup['name']} on {t['id']}; Rs {charge:,} refund claim drafted with GPS proof.")


def sentinel_report(fleet: Fleet) -> list[dict]:
    """Merged from Saarthi (Gnani build): the CEO Agent's always-on checklist - proves nothing is missed."""
    today = date.today()
    rows = []
    for t in fleet.trucks.values():
        issues = []
        for name, d in t["docs"].items():
            left = (date.fromisoformat(d) - today).days
            if left <= 15:
                issues.append(f"{name} expires in {left}d")
        if t["drive_hours"] >= 9:
            issues.append(f"driven {t['drive_hours']} h today")
        if t.get("wait_since_h", 0) > 4 and t["status"] == "waiting":
            issues.append(f"waiting {t['wait_since_h']} h at dock")
        if t["status"] == "breakdown":
            issues.append("breakdown in progress")
        if t["status"] in ("empty", "idle"):
            issues.append("no load - earning nothing")
        if t["fuel"] < 25:
            issues.append(f"fuel {t['fuel']}%")
        if t["health"] < 80:
            issues.append(f"engine health {t['health']}")
        rows.append({"truck": t["id"], "driver": t["driver"], "status": t["status"],
                     "ok": not issues, "issues": issues})
    return sorted(rows, key=lambda r: (r["ok"], r["truck"]))


async def pb_sentinel(e: Engine, target=None):
    rows = sentinel_report(e.fleet)
    bad = [r for r in rows if not r["ok"]]
    await e.say("sentinel", f"Sentinel round: checked {len(rows)} trucks × 7 checks (papers, hours, dock wait, breakdown, "
                f"empty, fuel, engine).")
    for r in bad[:5]:
        await e.say("sentinel", f"{r['truck']} ({r['driver']}): " + "; ".join(r["issues"]), kind="alert", truck=r["truck"], wait=0.7)
    await e.resolve("sentinel", f"Sentinel: {len(rows) - len(bad)}/{len(rows)} trucks all-clear", 0,
                    {"checked": len(rows), "flagged": len(bad)},
                    f"CEO Sentinel checked all {len(rows)} trucks: {len(rows) - len(bad)} all-clear, {len(bad)} flagged with an owner action each.")


PLAYBOOKS = {"dispatch": pb_dispatch, "breakdown": pb_breakdown, "empty_return": pb_empty,
             "delivery_refused": pb_refused, "docs_expiring": pb_docs, "cancel_after_dispatch": pb_cancel,
             "detention": pb_detention, "fatigue": pb_fatigue, "fastag_audit": pb_fastag, "sentinel": pb_sentinel}
