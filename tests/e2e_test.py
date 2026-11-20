"""End-to-end check of every Saarthi feature against the running server.
Usage: .venv\\Scripts\\python tests\\e2e_test.py [base_url]"""
import asyncio
import json
import sys
import time

import aiohttp

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
PLAYBOOKS = ["dispatch", "breakdown", "empty_return", "delivery_refused", "cancel_after_dispatch", "detention",
             "docs_expiring", "fatigue", "fastag_audit", "sentinel"]
QUESTIONS = ["Which truck made the least profit this month?", "Which trucks have bad diesel mileage?",
             "Empty kilometres by truck", "Weekly profit trend"]
CHATS = [("owner", None, "Fleet ka status batao"), ("owner", None, "Chittorgarh se Delhi ka route aur mausam batao"),
         ("driver", "RJ09GA4508", "Mera trip kitna baaki hai?"),
         ("driver", "RJ09GA4508", "Malik ko bolo ki unloading mein 2 ghante lagenge"),
         ("ceo", None, "RJ09GA4508 ke driver ko bolo ki Delhi pahunch ke call kare")]


async def main():
    results, events = [], []
    async with aiohttp.ClientSession() as s:
        await s.post(f"{BASE}/api/reset", json={})
        await s.post(f"{BASE}/api/pace", json={"pace": 0.2})
        health = await (await s.get(f"{BASE}/api/health")).json()
        print("Health:", json.dumps(health))

        async def listen():
            async with s.get(f"{BASE}/api/events", timeout=aiohttp.ClientTimeout(total=None)) as r:
                async for line in r.content:
                    if line.startswith(b"data: "):
                        d = json.loads(line[6:])
                        if d["type"] == "event":
                            events.append(d)
        lt = asyncio.create_task(listen())
        await asyncio.sleep(0.5)
        for pb in PLAYBOOKS:
            t0, n0 = time.time(), len(events)
            r = await (await s.post(f"{BASE}/api/playbook", json={"name": pb})).json()
            ok = False
            while time.time() - t0 < 60:
                new = events[n0:]
                if any(e["kind"] == "resolved" and (e.get("data") or {}).get("playbook") == pb for e in new):
                    ok = True
                    break
                if any("hit an error" in e["text"] for e in new):
                    break
                await asyncio.sleep(0.3)
            new = events[n0:]
            err = [e["text"] for e in new if "hit an error" in e["text"]]
            results.append((f"playbook:{pb}", ok and not err, f"{len(new)} steps, {time.time()-t0:.1f}s {err[:1]}"))
        for q in QUESTIONS:
            t0 = time.time()
            r = await (await s.post(f"{BASE}/api/analyst", json={"question": q})).json()
            results.append((f"analyst:{q[:30]}", bool(r.get("rows")) and bool(r.get("answer")),
                            f"{len(r.get('rows', []))} rows via {r.get('via')} {time.time()-t0:.1f}s"))
        for role, truck, text in CHATS:
            t0 = time.time()
            r = await (await s.post(f"{BASE}/api/chat", json={"text": text, "role": role, "truck": truck})).json()
            results.append((f"chat:{role}:{text[:24]}", bool(r.get("reply")), f"{r.get('reply','')[:70]!r} {time.time()-t0:.1f}s"))
        t0 = time.time()
        b = await (await s.get(f"{BASE}/api/ceo/brief")).json()
        results.append(("ceo:brief", bool(b.get("brief")) and len(b.get("proposals", [])) > 0,
                        f"{len(b['proposals'])} proposals, {b['brief'][:70]!r} {time.time()-t0:.1f}s"))
        a = await (await s.post(f"{BASE}/api/ceo/approve", json={"id": "service", "truck": b["proposals"][0].get("truck")})).json()
        results.append(("ceo:approve", a.get("ok", False), a.get("msg", "")))
        for text in ["Is mahine sabse zyada nuksaan kaunsa truck kar raha hai?", "Khaali truck ke liye wapsi ka load dhundo"]:
            t0 = time.time()
            r = await (await s.post(f"{BASE}/api/chat", json={"text": text, "role": "ceo"})).json()
            results.append((f"ceo:chat:{text[:22]}", bool(r.get("reply")),
                            f"tools={[x['tool'] for x in r.get('tools', [])]} {r.get('reply','')[:60]!r} {time.time()-t0:.1f}s"))
        for page in ["/", "/fleet", "/ceo", "/dash"]:
            rr = await s.get(f"{BASE}{page}")
            results.append((f"page:{page}", rr.status == 200, str(rr.status)))
        p = await (await s.get(f"{BASE}/api/predict?from=Jaipur&to=Mumbai")).json()
        results.append(("predict:Jaipur-Mumbai", p.get("km", 0) > 0, f"{p.get('km')} km, ETA {p.get('eta_hours')} h, {p.get('route_source')}, wx {p['weather'][0]['source']}"))
        results.append(("tolls:NHAI plazas", p["tolls"].get("count", 0) > 0 or "OSRM" not in p.get("route_source", ""),
                        f"{p['tolls'].get('count')} plazas, toll Rs {p['cost']['toll']:,} ({p['tolls'].get('source', '')[:40]})"))
        sn = await (await s.get(f"{BASE}/api/ceo/sentinel")).json()
        results.append(("ceo:sentinel", sn["total"] == 12, f"{sn['ok']}/{sn['total']} all-clear"))
        relays = [e for e in events if e["kind"] == "relay"]
        results.append(("relay:driver<->owner", len(relays) >= 2, f"{len(relays)} relay messages"))
        st = await (await s.get(f"{BASE}/api/state")).json()
        results.append(("state", len(st["trucks"]) == 12, f"kpis {st['kpis']}"))
        await s.post(f"{BASE}/api/pace", json={"pace": 1.0})
        lt.cancel()
    width = max(len(r[0]) for r in results)
    for name, ok, info in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {info}")
    fails = sum(not r[1] for r in results)
    print(f"\n{len(results) - fails}/{len(results)} passed")
    sys.exit(1 if fails else 0)


asyncio.run(main())
