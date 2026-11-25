"""Telegram front end."""

from __future__ import annotations

import html
import json
import logging
import uuid
from collections import OrderedDict

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import fmt
from .agent import Agent
from .arr import Radarr, Sonarr
from .config import Config
from .llm import ToolCall, make_provider
from .state import Store
from .tools import PendingAdd, ToolRunner
from .tracker import Tracker

log = logging.getLogger(__name__)

HELP = (
    "Schreib mir einfach, was du sehen willst, z. B.:\n"
    "• „Lad mir Dune Teil 2 runter“\n"
    "• „Staffel 1 und 2 von Dragonball Z“\n"
    "• „The Office, aber die amerikanische“\n"
    "• einen Link von IMDb, TMDB, Trakt, Letterboxd, JustWatch …\n"
    "• „Was läuft gerade?“\n\n"
    "Ich sage Bescheid, wenn ein Download startet, fertig ist oder Probleme hat.\n\n"
    "/status – aktuelle Downloads\n"
    "/neu – Gespräch zurücksetzen"
)

MAX_CHOICE_SETS = 200


def build_app(cfg: Config) -> Application:
    store = Store(cfg.data_dir)
    radarr = Radarr(cfg.radarr) if cfg.radarr else None
    sonarr = Sonarr(cfg.sonarr) if cfg.sonarr else None
    tools = ToolRunner(radarr, sonarr, store, cfg.minimum_availability, cfg.quality_prompt_seconds)
    agent = Agent(make_provider(cfg.llm), tools, cfg.history_idle_minutes, cfg.max_history_messages)
    allowed = filters.User(user_id=list(cfg.allowed_users))
    choice_sets: OrderedDict[str, list[str]] = OrderedDict()

    async def send_formatted(chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        """Send Markdown-ish text as Telegram HTML; keyboard goes on the last part."""
        parts = fmt.chunks(text)
        for i, part in enumerate(parts):
            kb = markup if i == len(parts) - 1 else None
            try:
                await app.bot.send_message(chat_id, fmt.to_html(part), parse_mode=ParseMode.HTML, reply_markup=kb)
            except BadRequest as e:
                log.warning("HTML abgelehnt (%s), sende als Text", e)
                await app.bot.send_message(chat_id, part, reply_markup=kb)

    async def send(chat_id: int, text: str) -> None:
        await send_formatted(chat_id, text)

    tracker = Tracker(radarr, sonarr, store, send, cfg.no_release_minutes, cfg.track_days)

    def allowed_user(update: Update) -> bool:
        return bool(update.effective_user and update.effective_user.id in cfg.allowed_users)

    # ---- conversation -----------------------------------------------------
    async def respond(chat_id: int, text: str, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            reply = await agent.handle(chat_id, text)
        except Exception:  # noqa: BLE001
            log.exception("Fehler bei Nachricht")
            reply = "⚠️ Da ist intern etwas schiefgelaufen. Details stehen im Log."

        markup = None
        if options := tools.take_buttons(chat_id):
            set_id = uuid.uuid4().hex[:8]
            choice_sets[set_id] = options
            while len(choice_sets) > MAX_CHOICE_SETS:
                choice_sets.popitem(last=False)
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(o, callback_data=f"c:{set_id}:{i}")] for i, o in enumerate(options)]
            )
        await send_formatted(chat_id, reply, markup)
        for p in tools.take_new_prompts(chat_id):
            await send_quality_prompt(p, ctx)

    async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await respond(update.effective_chat.id, update.effective_message.text, ctx)

    async def on_choice_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not allowed_user(update):
            await query.answer("Kein Zugriff.", show_alert=True)
            return
        _, set_id, idx = query.data.split(":")
        options = choice_sets.pop(set_id, None)
        if options is None:
            await query.answer("Diese Auswahl ist abgelaufen. Schreib mir einfach nochmal.")
            return
        choice = options[int(idx)]
        await query.answer()
        try:  # show the pick and remove the other buttons
            await query.edit_message_reply_markup(
                InlineKeyboardMarkup([[InlineKeyboardButton(f"✓ {choice}", callback_data="noop")]])
            )
        except BadRequest:
            pass
        await respond(update.effective_chat.id, choice, ctx)

    async def on_noop(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.callback_query.answer()

    # ---- commands -----------------------------------------------------------
    async def on_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text("Hallo! 🎬\n\n" + HELP)

    async def on_reset(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        agent.reset(update.effective_chat.id)
        await update.effective_message.reply_text("Okay, neues Gespräch.")

    async def on_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        res = await tools.run(ToolCall("status", "download_status", {}), chat_id)
        if res.is_error:
            await update.effective_message.reply_text(f"⚠️ {res.content}")
            return
        items = json.loads(res.content)
        if "info" in items[0]:
            await update.effective_message.reply_text(items[0]["info"])
            return
        rows = [["Titel", "%", "Rest"]]
        problems = []
        for it in items:
            pct = f"{it['progress_percent']}" if it["progress_percent"] is not None else "?"
            rows.append([("⚠ " if it.get("problems") else "") + it["title"], pct, (it.get("time_left") or "–")[:8]])
            problems += [f"⚠️ <b>{html.escape(it['title'])}</b>: {html.escape(p)}" for p in it.get("problems") or []]
        text = f"<b>⬇️ Downloads ({len(items)})</b>\n" + fmt.table_block(rows, max_col=24)
        if problems:
            text += "\n\n" + "\n".join(problems)
        await app.bot.send_message(chat_id, text[:4096], parse_mode=ParseMode.HTML)

    # ---- quality choice via buttons --------------------------------------
    async def send_quality_prompt(p: PendingAdd, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        buttons = [
            [InlineKeyboardButton(("⭐ " if name == p.default else "") + name, callback_data=f"q:{p.id}:{i}")]
            for i, name in enumerate(p.profiles)
        ]
        msg = await ctx.bot.send_message(
            p.chat_id,
            f"🎚 Welche Qualität für {p.title}?\n"
            f"Ohne Auswahl nehme ich in {cfg.quality_prompt_seconds} s „{p.default}“.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        p.message_id = msg.message_id
        ctx.job_queue.run_once(on_quality_timeout, cfg.quality_prompt_seconds, data=p.id, name=f"q:{p.id}")

    async def finish_quality(p: PendingAdd, profile: str, ctx: ContextTypes.DEFAULT_TYPE, note: str = "") -> None:
        await ctx.bot.edit_message_text(f"⏳ {p.title}: {note}{profile} …", p.chat_id, p.message_id)
        text = await tools.complete_pending(p, profile)
        await ctx.bot.edit_message_text(text, p.chat_id, p.message_id)

    async def on_quality_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not allowed_user(update):
            await query.answer("Kein Zugriff.", show_alert=True)
            return
        _, pid, idx = query.data.split(":")
        p = tools.pop_pending(pid)
        if p is None:
            await query.answer("Schon erledigt.")
            return
        await query.answer()
        for job in ctx.job_queue.get_jobs_by_name(f"q:{pid}"):
            job.schedule_removal()
        await finish_quality(p, p.profiles[int(idx)], ctx)

    async def on_quality_timeout(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        p = tools.pop_pending(ctx.job.data)
        if p is not None:
            await finish_quality(p, p.default, ctx, note="keine Auswahl, nehme Standard ")

    # ---- misc -------------------------------------------------------------
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
    app.add_handler(CallbackQueryHandler(on_quality_button, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(on_choice_button, pattern=r"^c:"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))
    app.add_handler(MessageHandler(~allowed, on_unauthorized))
    return app
