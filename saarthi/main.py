"""Run everything: Telegram bot(s) + fatigue scheduler + HTTP server (tracking page, n8n API).

    python -m saarthi.main
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiohttp import web

from . import db, telegram_app
from .bus import bus
from .config import settings
from .web import build_app


async def _fatigue_loop() -> None:
    """Every minute: companion check-ins + CEO watchdog (tolls, long stops, offline trucks)."""
    from . import business, fatigue, planner, sentinel, watchdog
    while True:
        await asyncio.sleep(60)
        for job in (fatigue.tick, watchdog.tick, sentinel.tick, business.tick, planner.monitor):
            try:
                await job()
            except Exception:
                logging.exception("%s failed", job.__module__)


def check_config() -> list[str]:
    problems = []
    if settings.speech_mode == "gnani" and not settings.gnani_api_key:
        problems.append("GNANI_API_KEY missing (live voice needs it)")
    if not settings.llm_api_key:
        problems.append("LLM_API_KEY missing (the agents need it to think and talk)")
    return problems


async def amain() -> None:
    db.conn()
    import os
    from .web import record_message
    bus.set_recorder(record_message)   # driver page + office show every message, whatever the channel
    apps = []
    if settings.driver_bot_token:  # Telegram: owner voice notes + driver fallback
        apps.append(telegram_app.build(settings.driver_bot_token, "driver"))
        if settings.owner_bot_token:
            apps.append(telegram_app.build(settings.owner_bot_token, "owner"))
        bus.set_sender(telegram_app.send)
    else:
        logging.warning("No DRIVER_BOT_TOKEN: Telegram off — owner alerts and driver messages go to the web pages")
        # web-only: give every owner/driver an inbox address so the agents' messages are produced and shown
        db.ex("UPDATE fleets SET owner_chat_id=900000+id WHERE owner_chat_id IS NULL")
        db.ex("UPDATE vehicles SET driver_chat_id=910000+id WHERE driver_chat_id IS NULL")
    asyncio.create_task(_fatigue_loop())
    if os.getenv("DEMO_DRIVE", "0") == "1":   # a SIMULATED truck drives NH48 so the CEO has something to watch
        from . import demo_drive
        asyncio.create_task(demo_drive.drive_forever())

    runner = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.http_port).start()
    logging.info("HTTP on :%s — driver companion /driver · office dashboard /office · n8n API /api", settings.http_port)

    for app in apps:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=["message", "edited_message", "callback_query"])
    logging.info("Saarthi running. Bots: %s. Speech: %s. LLM: %s", [a.bot.username for a in apps],
                 settings.speech_mode, settings.llm_model)
    try:
        await asyncio.Event().wait()
    finally:
        for app in apps:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        await runner.cleanup()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    problems = check_config()
    if problems and "--allow-missing-keys" not in sys.argv:
        print("Fix your .env first:\n- " + "\n- ".join(problems))
        sys.exit(1)
    for p in problems:
        logging.warning("%s — that part stays off until the key is added", p)
    asyncio.run(amain())


if __name__ == "__main__":
    main()
