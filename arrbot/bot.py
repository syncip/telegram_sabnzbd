"""Telegram front end."""

from __future__ import annotations

import json
import logging

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .agent import Agent
from .arr import Radarr, Sonarr
from .config import Config
from .llm import ToolCall, make_provider
from .state import Store
from .tools import ToolRunner
from .tracker import Tracker

log = logging.getLogger(__name__)

HELP = (
    "Schreib mir einfach, was du sehen willst, z. B.:\n"
    "• „Lad mir Dune Teil 2 runter“\n"
    "• „Die neueste Staffel von Severance“\n"
    "• „The Office, aber die amerikanische“\n"
    "• „Was läuft gerade?“\n\n"
    "Ich sage Bescheid, wenn ein Download startet, fertig ist oder Probleme hat.\n\n"
    "/status – aktuelle Downloads\n"
    "/neu – Gespräch zurücksetzen"
)


def _chunks(text: str, size: int = 4000) -> list[str]:
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


def build_app(cfg: Config) -> Application:
    store = Store(cfg.data_dir)
    radarr = Radarr(cfg.radarr) if cfg.radarr else None
    sonarr = Sonarr(cfg.sonarr) if cfg.sonarr else None
    tools = ToolRunner(radarr, sonarr, store, cfg.minimum_availability)
    agent = Agent(make_provider(cfg.llm), tools, cfg.history_idle_minutes, cfg.max_history_messages)
    allowed = filters.User(user_id=list(cfg.allowed_users))

    async def send(chat_id: int, text: str) -> None:
        for part in _chunks(text):
            await app.bot.send_message(chat_id, part)

    tracker = Tracker(radarr, sonarr, store, send, cfg.no_release_minutes, cfg.track_days)

    async def on_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text("Hallo! 🎬\n\n" + HELP)

    async def on_reset(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        agent.reset(update.effective_chat.id)
        await update.effective_message.reply_text("Okay, neues Gespräch.")

    async def on_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        res = await tools.run(ToolCall("status", "download_status", {}), update.effective_chat.id)
        if res.is_error:
            await update.effective_message.reply_text(f"⚠️ {res.content}")
            return
        lines = []
        for item in json.loads(res.content):
            if "info" in item:
                lines.append(item["info"])
                continue
            pct = f"{item['progress_percent']} %" if item["progress_percent"] is not None else "?"
            line = f"• {item['title']} – {pct}, {item['state']}"
            if item.get("time_left"):
                line += f", noch {item['time_left']}"
            for p in item.get("problems") or []:
                line += f"\n   ⚠️ {p}"
            lines.append(line)
        for part in _chunks("\n".join(lines)):
            await update.effective_message.reply_text(part)

    async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            reply = await agent.handle(chat_id, update.effective_message.text)
        except Exception:  # noqa: BLE001
            log.exception("Fehler bei Nachricht")
            reply = "⚠️ Da ist intern etwas schiefgelaufen. Details stehen im Log."
        for part in _chunks(reply):
            await update.effective_message.reply_text(part)

    async def on_unauthorized(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        log.warning("Zugriff verweigert für %s (%s)", user.id if user else "?", user.username if user else "?")
        if update.effective_message and user:
            await update.effective_message.reply_text(f"Kein Zugriff. Deine Telegram-ID ist {user.id}.")

    async def poll_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        await tracker.poll()

    async def post_init(application: Application) -> None:
        await application.bot.set_my_commands(
            [BotCommand("status", "Aktuelle Downloads"), BotCommand("neu", "Gespräch zurücksetzen"),
             BotCommand("hilfe", "Was kann ich?")]
        )
        application.job_queue.run_repeating(poll_job, interval=cfg.poll_interval, first=5)
        log.info("Bot läuft mit %s / %s", cfg.llm.provider, cfg.llm.model)

    async def post_shutdown(_: Application) -> None:
        for svc in (radarr, sonarr):
            if svc:
                await svc.close()

    app = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler(["start", "hilfe", "help"], on_start, filters=allowed))
    app.add_handler(CommandHandler(["neu", "reset"], on_reset, filters=allowed))
    app.add_handler(CommandHandler("status", on_status, filters=allowed))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(~allowed, on_unauthorized))
    return app
