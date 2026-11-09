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
        problems.append("GNANI_API_KEY missing")
    if not settings.llm_api_key:
        problems.append("LLM_API_KEY missing")
    return problems


async def amain() -> None:
    db.conn()
    apps = []
    if settings.driver_bot_token:  # Telegram: owner voice notes + driver fallback
        apps.append(telegram_app.build(settings.driver_bot_token, "driver"))
        if settings.owner_bot_token:
            apps.append(telegram_app.build(settings.owner_bot_token, "owner"))
        bus.set_sender(telegram_app.send)
    else:
        logging.warning("No DRIVER_BOT_TOKEN: Telegram disabled (owner alerts only on the office dashboard log)")
    asyncio.create_task(_fatigue_loop())

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
    if problems:
        print("Fix your .env first:\n- " + "\n- ".join(problems))
        sys.exit(1)
    asyncio.run(amain())


if __name__ == "__main__":
    main()
