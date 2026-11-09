"""HTTP server: client tracking page (no GPS hardware) + small API for n8n workflows."""
from __future__ import annotations

import html
import json
from pathlib import Path

from aiohttp import web

from . import db
from .agents import ceo
from .agents.common import mask_reg
from .bus import bus
from .config import settings

TRACK_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shipment tracking</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{margin:0;font-family:system-ui,sans-serif}#bar{padding:12px 16px;background:#0f3d3e;color:#fff}
#map{height:calc(100vh - 64px)}small{opacity:.8}</style></head><body>
<div id="bar"><b>__TITLE__</b><br><small id="st">loading…</small></div><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const map=L.map('map').setView([23.5,78.9],5);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:18,attribution:'© OpenStreetMap'}).addTo(map);
let m=null;
async function load(){try{const r=await fetch(location.pathname+'.json');const d=await r.json();
 if(d.lat==null){document.getElementById('st').textContent='Waiting for the driver to share location';return;}
 if(!m){m=L.marker([d.lat,d.lon]).addTo(map);map.setView([d.lat,d.lon],11);}else{m.setLatLng([d.lat,d.lon]);}
 document.getElementById('st').textContent=`${d.status} · last update ${new Date(d.last_seen).toLocaleString()}`;}catch(e){}}
load();setInterval(load,30000);
</script></body></html>"""


def _trip(token: str) -> dict | None:
    return db.one("SELECT t.*, v.reg_label, v.last_lat, v.last_lon, v.last_seen, v.last_speed_kmph FROM trips t "
                  "JOIN vehicles v ON v.id=t.vehicle_id WHERE t.track_token=?", (token,))


async def track_page(req: web.Request) -> web.Response:
    t = _trip(req.match_info["token"])
    if not t:
        raise web.HTTPNotFound()
    title = html.escape(f"{t['origin']} → {t['destination']} · {mask_reg(t['reg_label'])}")
    return web.Response(text=TRACK_HTML.replace("__TITLE__", title), content_type="text/html")


async def track_json(req: web.Request) -> web.Response:
    t = _trip(req.match_info["token"])
    if not t:
        raise web.HTTPNotFound()
    if t["status"] != "active":
        return web.json_response({"lat": None, "status": "delivered / trip closed"})
    moving = (t["last_speed_kmph"] or 0) >= settings.moving_speed_kmph
    return web.json_response({"lat": t["last_lat"], "lon": t["last_lon"], "last_seen": t["last_seen"],
                              "status": "moving" if moving else "stopped"})


def _auth(req: web.Request) -> None:
    if req.headers.get("X-Saarthi-Key") != settings.api_shared_secret or settings.api_shared_secret == "change_me":
        raise web.HTTPUnauthorized(text="set API_SHARED_SECRET and send it as X-Saarthi-Key")


async def api_digest(req: web.Request) -> web.Response:
    _auth(req)
    out = []
    for f in db.q("SELECT * FROM fleets"):
        text = await ceo.daily_digest(f)
        if req.query.get("send") == "1":
            o = ceo.owner_out(f, text, speak=None)
            if o:
                await bus.send(o)
        out.append({"fleet": f["code"], "digest": text})
    return web.json_response(out)


async def api_expenses(req: web.Request) -> web.Response:
    _auth(req)
    since = req.query.get("since", "1970-01-01")
    rows = db.q("SELECT e.id, e.created_at, f.name fleet, v.reg_label, e.category, e.amount, e.litres, e.note, e.lat, e.lon "
                "FROM expenses e JOIN vehicles v ON v.id=e.vehicle_id JOIN fleets f ON f.id=v.fleet_id "
                "WHERE e.status='confirmed' AND e.created_at>=? ORDER BY e.id", (since,))
    return web.json_response(rows)


async def api_documents(req: web.Request) -> web.Response:
    _auth(req)
    days = int(req.query.get("days", "30"))
    res = []
    for f in db.q("SELECT * FROM fleets"):
        tools = {t.name: t for t in ceo._fleet_tools(f["id"])}
        docs = await tools["documents_expiring"].fn(days=days)
        if docs and req.query.get("send") == "1":
            lines = "\n".join(f"• {d['reg_label']} {d['doc_type']}: {d['expiry_date']}" for d in docs)
            o = ceo.owner_out(f, f"📄 Documents expiring within {days} days:\n{lines}")
            if o:
                await bus.send(o)
        res.append({"fleet": f["code"], "expiring": docs})
    return web.json_response(res)


async def api_agent_log(req: web.Request) -> web.Response:
    _auth(req)
    rows = db.q("SELECT * FROM agent_log ORDER BY id DESC LIMIT ?", (int(req.query.get("limit", "50")),))
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    return web.json_response(rows)


STATIC = Path(__file__).parent / "static"


async def driver_page(req: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "driver.html")


async def office_page(req: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "office.html")


async def api_offer(req: web.Request) -> web.Response:
    v = db.vehicle_by_token(req.query.get("v", ""), req.query.get("k", ""))
    if not v:
        raise web.HTTPUnauthorized(text="unknown vehicle or wrong key")
    from . import companion
    answer = await companion.offer(v, await req.json())
    return web.json_response(answer)


async def api_ice(req: web.Request) -> web.Response:
    if not db.vehicle_by_token(req.query.get("v", ""), req.query.get("k", "")):
        raise web.HTTPUnauthorized()
    return web.json_response(settings.ice_servers)


async def api_offer_patch(req: web.Request) -> web.Response:
    from . import companion
    await companion.ice(await req.json())
    return web.json_response({"status": "success"})


async def api_fleet(req: web.Request) -> web.Response:
    _auth(req)
    out = []
    for f in db.q("SELECT * FROM fleets ORDER BY id"):
        vs = []
        for v in db.q("SELECT * FROM vehicles WHERE fleet_id=? ORDER BY code", (f["id"],)):
            t = db.active_trip(v["id"])
            inc = db.one("SELECT description FROM incidents WHERE vehicle_id=? AND status='open' ORDER BY id DESC LIMIT 1", (v["id"],))
            vs.append({"code": v["code"], "reg": v["reg_label"], "driver": v["driver_name"], "lat": v["last_lat"],
                       "lon": v["last_lon"], "last_seen": v["last_seen"],
                       "trip": f"{t['origin']} → {t['destination']}" if t else None,
                       "companion": "live" if v["id"] in bus.live else "offline",
                       "issue": inc["description"] if inc else None,
                       "speed": round(v["last_speed_kmph"] or 0),
                       "tolls_unreported": db.one("SELECT COUNT(*) n FROM toll_crossings WHERE vehicle_id=? AND status IN "
                                                  "('unreported','asked')", (v["id"],))["n"],
                       "trip_km": round(t["gps_km"] or 0) if t else None,
                       "plan": _plan_brief(t) if t else None})
        out.append({"code": f["code"], "name": f["name"], "vehicles": vs})
    return web.json_response(out)


def _plan_brief(t: dict) -> dict | None:
    from . import planner
    p = planner.latest_plan(t["id"])
    if not p:
        return None
    nxt = next((s for s in p["stops"] if s["at_km"] > (t["gps_km"] or 0) - p.get("start_gps_km", 0)), None)
    return {"depart": p["depart"], "arrive": p["arrive"], "stops": len(p["stops"]),
            "next_stop": f"{nxt['kind'].replace('_', ' ')} @ km {nxt['at_km']}" + (f" — {nxt['place'].split(' (')[0]}" if nxt and nxt.get("place") else "") if nxt else None,
            "profit": p.get("profit")}


async def api_plan_fleet(req: web.Request) -> web.Response:
    _auth(req)
    from . import planner
    f = db.one("SELECT * FROM fleets WHERE upper(code)=upper(?)", (req.query.get("fleet", ""),)) or db.one("SELECT * FROM fleets")
    return web.json_response(planner.plan_fleet(f["id"]))


async def api_ceo_chat(req: web.Request) -> web.Response:
    _auth(req)
    body = await req.json()
    f = db.one("SELECT * FROM fleets WHERE upper(code)=upper(?)", (body.get("fleet", ""),))
    if not f or not body.get("text"):
        return web.json_response({"error": "fleet and text required"}, status=400)
    return web.json_response({"reply": await ceo.handle_owner(f, body["text"])})


async def api_fastag_statement(req: web.Request) -> web.Response:
    """Owner uploads the FASTag statement (CSV) from the bank / IHMCL portal."""
    _auth(req)
    from . import fastag
    if req.content_type.startswith("multipart/"):
        form = await req.post()
        f = form.get("file")
        text = f.file.read().decode("utf-8-sig", errors="replace") if hasattr(f, "file") else str(f or "")
    else:
        text = (await req.read()).decode("utf-8-sig", errors="replace")
    rows = fastag.parse_statement(text)
    results = [await fastag.reconcile(r) for r in rows]
    summary = {k: sum(1 for r in results if r["status"] == k) for k in ("matched", "suspect", "unknown_vehicle")}
    return web.json_response({"rows": len(rows), **summary, "results": results})


async def api_fastag_sms(req: web.Request) -> web.Response:
    """FASTag debit SMS: from the driver app (truck key) or forwarded from the owner's phone (office key)."""
    truck = db.vehicle_by_token(req.query.get("v", ""), req.query.get("k", "")) if req.query.get("k") else None
    if not truck:
        _auth(req)
    from . import fastag
    body = await req.json()
    d = fastag.parse_sms(body.get("text", ""))
    if not d:
        return web.json_response({"status": "not_a_fastag_debit"})
    default = truck or (fastag.find_vehicle(body.get("vehicle", "")) if body.get("vehicle") else None)
    return web.json_response(await fastag.reconcile(d, default_vehicle=default))


POD_DIR = Path(__file__).resolve().parent.parent / "data" / "pod"
MAX_POD_BYTES = 8 * 1024 * 1024


async def api_pod(req: web.Request) -> web.Response:
    """Driver's phone uploads the POD photo (receiving copy / bilty) for the delivered trip."""
    v = db.vehicle_by_token(req.query.get("v", ""), req.query.get("k", ""))
    if not v:
        raise web.HTTPUnauthorized()
    from . import business
    trip = db.one("SELECT * FROM trips WHERE vehicle_id=? AND delivered_at IS NOT NULL ORDER BY id DESC LIMIT 1", (v["id"],))
    if not trip:
        return web.json_response({"error": "no delivered trip"}, status=400)
    form = await req.post()
    f = form.get("file")
    if not hasattr(f, "file"):
        return web.json_response({"error": "file missing"}, status=400)
    data = f.file.read(MAX_POD_BYTES + 1)
    if len(data) > MAX_POD_BYTES:
        return web.json_response({"error": "photo too large"}, status=413)
    POD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"trip{trip['id']}_{int(__import__('time').time())}.jpg"
    (POD_DIR / name).write_bytes(data)
    inv = business.pod_received(trip["id"], name)
    if inv:
        from .agents import ceo
        o = ceo.owner_out(db.fleet(v["fleet_id"]), "🧾 POD received. Invoice ready to send:\n\n" + business.invoice_text(inv))
        if o:
            await bus.send(o)
    return web.json_response({"ok": True, "invoice": inv["id"] if inv else None})


async def api_location(req: web.Request) -> web.Response:
    """Native GPS from the driver app (works with the screen off)."""
    v = db.vehicle_by_token(req.query.get("v", ""), req.query.get("k", ""))
    if not v:
        raise web.HTTPUnauthorized()
    b = await req.json()
    lat, lon = float(b["lat"]), float(b["lon"])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return web.json_response({"error": "bad coordinates"}, status=400)
    from . import fatigue
    speed = b.get("speed")
    await fatigue.location_update(v["id"], lat, lon, float(speed) * 3.6 if isinstance(speed, (int, float)) else None)
    return web.json_response({"ok": True})


async def api_business(req: web.Request) -> web.Response:
    _auth(req)
    from . import business
    f = db.one("SELECT * FROM fleets WHERE upper(code)=upper(?)", (req.query.get("fleet", ""),)) or db.one("SELECT * FROM fleets")
    return web.json_response({"fleet": f["code"], **business.summary(f["id"])})


async def api_sentinel(req: web.Request) -> web.Response:
    _auth(req)
    from . import sentinel
    f = db.one("SELECT * FROM fleets WHERE upper(code)=upper(?)", (req.query.get("fleet", ""),)) or db.one("SELECT * FROM fleets")
    return web.json_response({"fleet": f["code"], **sentinel.fleet_health(f["id"])})


async def health(req: web.Request) -> web.Response:
    return web.json_response({"ok": True, "live_companions": len(bus.live)})


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/track/{token}.json", track_json),
        web.get("/track/{token}", track_page),
        web.get("/api/digest", api_digest),
        web.get("/api/expenses", api_expenses),
        web.get("/api/documents", api_documents),
        web.get("/api/agent-log", api_agent_log),
        web.get("/health", health),
        web.get("/driver", driver_page),
        web.get("/office", office_page),
        web.post("/api/offer", api_offer),
        web.patch("/api/offer", api_offer_patch),
        web.get("/api/ice", api_ice),
        web.get("/api/fleet", api_fleet),
        web.post("/api/ceo/chat", api_ceo_chat),
        web.post("/api/fastag/statement", api_fastag_statement),
        web.post("/api/fastag/sms", api_fastag_sms),
        web.post("/api/pod", api_pod),
        web.post("/api/location", api_location),
        web.get("/api/business", api_business),
        web.get("/api/sentinel", api_sentinel),
        web.get("/api/plan/fleet", api_plan_fleet),
        web.static("/static", STATIC),
    ])
    return app
