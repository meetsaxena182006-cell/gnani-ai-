"""One link to REVIEW the whole system (CEO office + driver side) — no API keys needed.

    Windows: double-click review_demo.bat        Others: python scripts/review_demo.py

What it does (everything is SYNTHETIC demo data, clearly labelled):
1. builds the demo day in a separate database (data/review_demo.db; your real data/saarthi.db is never touched);
2. a demo truck (RJ00 DEMO 03, driver "Kishan (demo)", cash tolls) drives Udaipur -> Ahmedabad on NH48 in
   ~8 minutes, again and again. The CEO watchdog sees the NHAI toll plazas it crosses, the CEO Agent asks the
   driver through his companion "kitna toll laga?", the Sentinel re-asks, then tells the owner;
3. opens a public HTTPS link (Cloudflare quick tunnel) to a review page:
   left = the real CEO office dashboard, right = what the driver's companion says and what the owner receives.

Review speed-up (labelled on the page): the Sentinel follows up after 2 minutes instead of 30.
Live voice and the AI chat need GNANI_API_KEY + LLM_API_KEY: use start_public.bat for that.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.getenv("PORT") or os.getenv("REVIEW_PORT", "8090"))  # cloud hosts set PORT
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ["DB_PATH"] = str(ROOT / "data" / "review_demo.db")
os.environ["SPEECH_MODE"] = "mock"
KEY = os.getenv("REVIEW_KEY") or secrets.token_urlsafe(8)
os.environ["API_SHARED_SECRET"] = KEY

from aiohttp import web  # noqa: E402

from saarthi import db, demo_story, sentinel, watchdog  # noqa: E402
from saarthi.bus import Out, bus  # noqa: E402
from saarthi.config import settings  # noqa: E402

settings.db_path = os.environ["DB_PATH"]
settings.api_shared_secret = KEY
sentinel.FOLLOWUP_MIN = 2  # review speed-up, shown on the page

from saarthi.demo_drive import SIM_CODE, drive_forever  # noqa: E402


# every message the system sends is shown on the review page
async def review_sender(out: Out) -> None:
    v = db.vehicle(out.vehicle_id) if out.vehicle_id else None
    db.log_agent("system", f"driver:{v['code']}" if v else "owner", "message",
                 {"text": out.text, "spoken": out.speak, "vehicle": v["code"] if v else None})


async def ticks() -> None:
    while True:
        await asyncio.sleep(20)
        for job in (watchdog.tick, sentinel.tick):
            try:
                await job()
            except Exception:
                import logging
                logging.exception("tick failed")


def _auth(req: web.Request) -> None:
    if req.headers.get("X-Saarthi-Key") != KEY:
        raise web.HTTPUnauthorized()


async def review_page(req: web.Request) -> web.FileResponse:
    return web.FileResponse(ROOT / "saarthi" / "static" / "review.html")


async def review_feed(req: web.Request) -> web.Response:
    _auth(req)
    since = int(req.query.get("since", "0") or 0)
    rows = db.q("SELECT id, ts, from_agent, to_agent, kind, payload FROM agent_log WHERE id>? AND "
                "(kind IN ('message','demo_lap') OR kind LIKE 'watchdog_%') ORDER BY id LIMIT 300", (since,))
    for r in rows:
        r["payload"] = json.loads(r["payload"] or "{}")
    v = db.vehicle_by_code(SIM_CODE)
    links = [{"code": x["code"], "reg": x["reg_label"], "driver": x["driver_name"],
              "url": f"{settings.public_base_url}/driver?v={x['code']}&k={x['access_token']}"}
             for x in db.q("SELECT * FROM vehicles ORDER BY code")]
    return web.json_response({"rows": rows, "sim": {"code": SIM_CODE, "reg": v["reg_label"]}, "drivers": links,
                              "followup_min": sentinel.FOLLOWUP_MIN})


def cloudflared() -> str | None:
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    local = ROOT / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    return str(local) if local.exists() else None


def open_tunnel() -> str | None:
    exe = cloudflared()
    if not exe:
        print("cloudflared not found: run start_public.bat once (it downloads it), or install it. Local link only.")
        return None
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", proc.stdout.readline() or "")
        if m:
            threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
            return m.group(0)
    print("Could not open the public tunnel; local link only.")
    return None


async def main() -> None:
    # on a cloud host the public URL is given (PUBLIC_BASE_URL, or Render's RENDER_EXTERNAL_URL): no tunnel needed
    hosted = os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
    url = hosted or (None if "--local" in sys.argv or os.getenv("REVIEW_PUBLIC", "1") != "1" else open_tunnel())
    settings.public_base_url = url or f"http://localhost:{PORT}"
    await demo_story.story()
    # synthetic chat ids so every owner alert and driver message is produced and shown on the review page
    db.ex("UPDATE fleets SET owner_chat_id=900000+id, owner_name='Owner (demo)'")
    db.ex("UPDATE vehicles SET driver_chat_id=910000+id")
    db.ex("UPDATE vehicles SET driver_name='Kishan (demo)' WHERE code=?", (SIM_CODE,))
    bus.set_sender(review_sender)
    from saarthi.web import build_app
    app = build_app()
    app.router.add_get("/review", review_page)
    app.router.add_get("/", review_page)
    app.router.add_get("/api/review/feed", review_feed)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    link = f"{settings.public_base_url}/review?k={KEY}"
    (ROOT / "data" / "review_link.txt").write_text(link + "\n", encoding="utf-8")
    print("\n" + "=" * 70 + f"\nREVIEW LINK (CEO office + driver side):\n  {link}\n" + "=" * 70, flush=True)
    asyncio.create_task(ticks())
    await drive_forever()


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
