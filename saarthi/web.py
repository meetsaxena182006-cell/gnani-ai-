"""HTTP server: client tracking page (no GPS hardware) + small API for n8n workflows."""
from __future__ import annotations

import html
import json

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


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/track/{token}.json", track_json),
        web.get("/track/{token}", track_page),
        web.get("/api/digest", api_digest),
        web.get("/api/expenses", api_expenses),
        web.get("/api/documents", api_documents),
        web.get("/api/agent-log", api_agent_log),
        web.get("/health", lambda r: web.json_response({"ok": True})),
    ])
    return app
