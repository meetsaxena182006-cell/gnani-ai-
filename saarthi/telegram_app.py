"""Telegram front-end. Driver bot (one bot, many trucks) + optional separate owner bot.
Voice notes -> Gnani Prisma -> agent -> reply text + Gnani Timbre voice note."""
from __future__ import annotations

import io
import logging
import os
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from . import db, fatigue
from .agents import ceo, driver, mesh
from .bus import Out, bus
from .config import settings
from .speech import MAX_CLIP_SECONDS, get_speech

log = logging.getLogger("saarthi.tg")
SAVE_AUDIO = os.getenv("SAVE_AUDIO", "0") == "1"   # only with the driver's consent (pilot)
apps: dict[str, Application] = {}


# ---------------- outgoing ----------------
async def send(out: Out) -> None:
    app = apps.get(out.channel) or apps["driver"]
    bot = app.bot
    markup = None
    if out.buttons:
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(label, callback_data=target[3:]) if target.startswith("cb:")
            else InlineKeyboardButton(label, url=target)] for label, target in out.buttons])
    await bot.send_message(out.chat_id, out.text, reply_markup=markup, disable_web_page_preview=True)
    if out.speak:
        audio = await get_speech().synthesize(out.speak)
        if audio:
            await bot.send_voice(out.chat_id, voice=io.BytesIO(audio), filename="saarthi.ogg")
    for name, phone in out.contacts:
        await bot.send_contact(out.chat_id, phone_number=phone, first_name=name[:60])
    if out.location:
        await bot.send_location(out.chat_id, latitude=out.location[0], longitude=out.location[1])


# ---------------- helpers ----------------
async def transcribe_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE, who: str) -> str | None:
    voice = update.message.voice or update.message.audio
    if voice.duration and voice.duration > MAX_CLIP_SECONDS:
        await update.message.reply_text("🙏 30 second se chhota voice note bhejiye (ek baat ek baar).")
        return None
    f = await ctx.bot.get_file(voice.file_id)
    if SAVE_AUDIO:
        d = Path("data/audio") / who
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{update.message.message_id}.ogg"
    else:
        path = Path(tempfile.mkstemp(suffix=".ogg")[1])
    await f.download_to_drive(str(path))
    try:
        res = await get_speech().transcribe(path)
    except Exception:
        log.exception("Prisma STT failed")
        await update.message.reply_text("Awaaz samajh nahi aayi (network). Dobara bhejiye.")
        return None
    finally:
        if not SAVE_AUDIO:
            path.unlink(missing_ok=True)
    text = (res.get("transcript") or "").strip()
    log.info("STT %s: %r (lang=%s, %.2fs)", who, text, res.get("detected_language_code"), res.get("latency_s", 0))
    await update.message.reply_text(f"🗣️ {text or '…'}")  # show what Prisma heard (transparency)
    return text


def role(chat_id: int) -> tuple[str, dict | None]:
    v = db.vehicle_by_chat(chat_id)
    if v:
        return "driver", v
    f = db.fleet_by_owner_chat(chat_id)
    if f:
        return "owner", f
    return "unknown", None


# ---------------- handlers ----------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    arg = (ctx.args[0] if ctx.args else "").strip()
    if arg.upper().startswith("OWNER-"):
        f = db.one("SELECT * FROM fleets WHERE upper(code)=upper(?)", (arg[6:],))
        if not f:
            await update.message.reply_text("Fleet code galat hai.")
            return
        db.ex("UPDATE fleets SET owner_chat_id=? WHERE id=?", (chat, f["id"]))
        await update.message.reply_text(
            f"✅ Namaste! Main {f['name']} ka CEO Agent hoon. Poochiye: 'sab gaadiyan kahan hain?', "
            f"'aaj ka kharcha?', 'Udaipur se Ahmedabad 16 ton ka quote do', 'kaun se documents expire ho rahe hain?'")
        return
    if arg:
        v = db.vehicle_by_code(arg)
        if not v:
            await update.message.reply_text("Gaadi ka code galat hai. Malik se code lijiye.")
            return
        name = update.effective_user.first_name
        db.ex("UPDATE vehicles SET driver_chat_id=?, driver_name=COALESCE(driver_name, ?) WHERE id=?", (chat, name, v["id"]))
        msg = (f"✅ Ram Ram! Main Saarthi hoon, aapka co-driver. Gaadi {v['reg_label']} jud gayi.\n"
               f"• Kharcha bolo: 'diesel 4500 ka daala, 48 litre'\n• Gaadi kharab ho to bolo: 'tyre phat gaya'\n"
               f"• 📎 → Location → 'Share Live Location' chalu rakhiye.")
        await update.message.reply_text(msg)
        return
    await update.message.reply_text("Code ke saath start kariye: /start <GAADI-CODE> (driver) ya /start OWNER-<FLEET> (malik)")


async def on_voice_or_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    r, obj = role(chat)
    if r == "unknown":
        await update.message.reply_text("Pehle /start <code> se judiye.")
        return
    if update.message.voice or update.message.audio:
        text = await transcribe_voice(update, ctx, f"{r}_{chat}")
        if text is None:
            return
    else:
        text = update.message.text or ""
    await ctx.bot.send_chat_action(chat, "typing")
    if r == "driver":
        await driver.handle(db.vehicle(obj["id"]), text)
    else:
        reply = await ceo.handle_owner(obj, text)
        await bus.send(Out(chat_id=chat, text=reply, channel="owner" if settings.owner_bot_token else "driver",
                           speak=reply if (update.message.voice and len(reply) < 400) else None))


async def on_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_message or update.message
    v = db.vehicle_by_chat(msg.chat_id)
    if not v or not msg.location:
        return
    first = v["last_lat"] is None
    fatigue.update_location(v["id"], msg.location.latitude, msg.location.longitude)
    if update.message and (first or not msg.location.live_period):
        await msg.reply_text("📍 Location mil gayi ✅")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    r, obj = role(update.effective_chat.id)
    if r != "owner":
        await update.message.reply_text("Yeh command sirf malik ke liye hai.")
        return
    await update.message.reply_text(await ceo.daily_digest(obj))


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    v = db.vehicle_by_chat(q.message.chat_id)
    parts = (q.data or "").split(":")
    if not v:
        return
    if parts[0] == "fatigue":
        fatigue.ack_if_pending(v["id"])
        await q.edit_message_reply_markup(None)
        await bus.send(Out(chat_id=v["driver_chat_id"], text="👍 Dhyan se chalaiye.", speak="Dhyan se chalaiye."))
    elif parts[0] == "mesh" and len(parts) == 3:
        await q.edit_message_reply_markup(None)
        reply = await mesh.respond(v, int(parts[2]), parts[1] == "accept")
        loc = mesh.requester_location(int(parts[2])) if parts[1] == "accept" else None
        await bus.send(Out(chat_id=v["driver_chat_id"], text=reply, speak=reply,
                           location=loc if loc and "Kisi aur" not in reply else None))


async def fatigue_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await fatigue.tick()


def build(token: str, name: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    # LOCATION covers new pins and live-location edits (edited_message)
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & (filters.VOICE | filters.AUDIO | filters.TEXT)
                                   & ~filters.COMMAND, on_voice_or_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    apps[name] = app
    return app
