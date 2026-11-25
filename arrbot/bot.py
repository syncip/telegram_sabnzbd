"""Telegram front end."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import downloads, fmt
from .agent import Agent
from .arr import ArrError, Radarr, Sonarr
from .config import Config
from .llm import ToolCall, make_provider
from .sabnzbd import SABnzbd, SabError
from .state import Store, Tracked
from .tmdb import TMDB
from .tools import Flow, Offer, ToolRunner, View, release_languages
from .tracker import Tracker
from . import extras, health, media, plex, profiles, stats, watch
from .profiles import ProfileStore
from .agent import PRICES
from .users import ADMIN, BLOCKED, LIMIT_KEYS, PENDING, USER, UserStore, limit_text

log = logging.getLogger(__name__)

HELP = (
    "Schreib mir einfach, was du sehen willst, z. B.:\n"
    "• „Lad mir Dune Teil 2 runter“\n"
    "• „Staffel 1 und 2 von Dragonball Z“\n"
    "• „Was soll ich als Nächstes schauen?“\n"
    "• „Welche Filme waren die Top 5 2024?“\n"
    "• „Lade die Top 3 Filme vom Oktober 2025“\n"
    "• „Gibt es Severance schon auf Deutsch?“\n"
    "• „Such in 3 Tagen nochmal nach Dune 3“ / „Lösch The Office“\n"
    "• „Merk dir, ich mag keine Horrorfilme“ / „Was weißt du über mich?“\n"
    "• „Sag mir, wenn Dune 3 auf Deutsch kommt“ / „Wo war ich bei Dark?“\n"
    "• „Schick mir jeden Sonntag einen Wochenrückblick“\n"
    "• einen Link von IMDb, TMDB, Trakt, Letterboxd, JustWatch …\n\n"
    "Ich sage Bescheid, wenn ein Download startet, wann er fertig ist und ob es Probleme gibt.\n\n"
    "/downloads – deine Downloads mit Fortschritt\n"
    "/verbrauch – deine Anfragen und Limits\n"
    "/profil – was ich über deinen Geschmack weiß\n"
    "/kalender – was diese Woche kommt\n"
    "/plex – Plex verbinden (gesehen, weiterschauen)\n"
    "/neu – Gespräch zurücksetzen"
)
ADMIN_HELP = (
    "\n\n<b>Admin</b>\n"
    "/nutzer – Nutzer freigeben, sperren, entfernen\n"
    "/statistik – Nutzer, Anfragen, Tokens, Kosten\n"
    "/mediathek – was fehlt, was nicht in Plex ist, was gesehen oder lange ungesehen ist (aufräumen)\n"
    "/check – alle APIs prüfen\n"
    "/limit – Limits setzen, z. B.\n"
    "<code>/limit standard tag 10</code>\n"
    "<code>/limit Anna monat 50</code>\n"
    "<code>/limit gesamt kosten 1</code>\n"
    "<code>/limit 123456 tokens aus</code>\n\n"
    "Oder einfach schreiben: „Zeig mir die Statistik“, „Setz Annas Tageslimit auf 5“, "
    "„Sperr Bob“, „Prüf die APIs“, „Was kann weg?“, "
    "„Neues Qualitätsprofil 4K nur Englisch“, „Profil 1080p Deutsch und Englisch + Originalsprache“."
)

RESPONSE_TIMEOUT = 180  # seconds for one whole answer, including all AI steps
ROLE_LABEL = {ADMIN: "👑 Admin", USER: "👤 Nutzer", PENDING: "⏳ wartet", BLOCKED: "🚫 gesperrt"}


def _display_name(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "?"
    return u.full_name or (f"@{u.username}" if u.username else str(u.id))


def build_app(cfg: Config) -> Application:
    tz = ZoneInfo(cfg.timezone)
    store = Store(cfg.data_dir)
    users = UserStore(cfg.data_dir, cfg.admin_id, cfg.allowed_users)
    radarr = Radarr(cfg.radarr) if cfg.radarr else None
    sonarr = Sonarr(cfg.sonarr) if cfg.sonarr else None
    sab = SABnzbd(cfg.sabnzbd_url, cfg.sabnzbd_api_key) if cfg.sabnzbd_url and cfg.sabnzbd_api_key else None
    tmdb = TMDB(cfg.tmdb_api_key) if cfg.tmdb_api_key else None
    tools = ToolRunner(radarr, sonarr, store, cfg.minimum_availability, cfg.quality_prompt_seconds, tmdb, tz)
    agent = Agent(make_provider(cfg.llm), tools, cfg.history_idle_minutes, cfg.max_history_messages)
    agent.on_usage = users.add_usage
    profile_db = ProfileStore(cfg.data_dir)
    tools.profiles = profile_db
    plex_store = plex.PlexStore(cfg.data_dir)
    plex_client = plex.Plex(plex_store.client_id(), cfg.plex_url)
    tools.plex, tools.plex_store = plex_client, plex_store

    def on_user_removed(uid: int) -> None:
        profile_db.forget_all(uid)
        plex_store.remove(uid)

    users.on_remove = on_user_removed
    background: set[asyncio.Task] = set()  # keeps running Plex links alive
    agent.context_for = lambda uid: profile_db.profile(uid).context_line()

    def on_finish(t, title: str) -> None:
        kind = "movie" if t.service == "radarr" else "series"
        for uid in t.chat_ids:
            profile_db.learn(uid, "finished", kind, title)

    store.on_finish = on_finish

    class Allowed(filters.MessageFilter):
        def filter(self, message: Message) -> bool:
            return bool(message.from_user and users.is_allowed(message.from_user.id))

    class IsAdmin(filters.MessageFilter):
        def filter(self, message: Message) -> bool:
            return bool(message.from_user and users.is_admin(message.from_user.id))

    allowed, is_admin = Allowed(), IsAdmin()

    def allowed_user(update: Update) -> bool:
        return bool(update.effective_user and users.is_allowed(update.effective_user.id))

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

    async def send(chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        markup = None
        if buttons:  # two per row keeps them readable on a phone
            rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(l, callback_data=d) for l, d in r] for r in rows])
        await send_formatted(chat_id, text, markup)

    async def tell_admins(text: str, markup: InlineKeyboardMarkup | None = None, html_text: bool = False) -> None:
        for admin in users.admins():
            try:
                if html_text:
                    await app.bot.send_message(admin, text, parse_mode=ParseMode.HTML, reply_markup=markup)
                else:
                    await app.bot.send_message(admin, text, reply_markup=markup)
            except (BadRequest, Forbidden) as e:
                log.warning("Admin %s nicht erreichbar: %s", admin, e)

    async def on_added(chat_id: int, text: str) -> None:
        if not users.is_admin(chat_id):
            await tell_admins(f"📣 {users.name(chat_id)}: {text}")

    async def no_release_hint(t: Tracked) -> str | None:
        """Why nothing was found: no German version yet (TMDB) or only non-German releases (indexers)."""
        parts = []
        if t.service == "radarr" and radarr:
            movie = await radarr.get(t.arr_id)
            if line := await tools._german_line("movie", movie.get("tmdbId")):
                parts.append(line)
            try:
                rel = release_languages(await radarr.releases(movieId=t.arr_id))
            except ArrError:
                rel = None
            if rel and rel["releases"] and not rel["deutsch"]:
                parts.append(f"🗣 Es gibt {rel['releases']} Releases, aber keins auf Deutsch.")
        elif t.service == "sonarr" and sonarr:
            series = await sonarr.get(t.arr_id)
            if series.get("tvdbId") and (line := await tools._german_line("series", series["tvdbId"])):
                parts.append(line)
        return "\n".join(parts) or None

    tools.on_added = on_added
    tools.is_admin = users.is_admin
    tools.users = users

    async def notify_user(uid: int, text: str) -> None:
        try:
            await app.bot.send_message(uid, text)
        except (BadRequest, Forbidden) as e:
            log.info("Nachricht an %s nicht zugestellt: %s", uid, e)

    tools.notify_user = notify_user
    tracker = Tracker(radarr, sonarr, store, send, cfg.no_release_minutes, cfg.track_days, sab=sab, tz=tz,
                      hint=no_release_hint)

    # ---- conversation -----------------------------------------------------
    async def keep_typing(chat_id: int) -> None:
        # Telegram shows "typing…" for 5 s only; refresh it until the answer is ready.
        while True:
            try:
                await app.bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(4)

    async def respond(chat_id: int, user_id: int, text: str, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.info("chat %s: %r", chat_id, text[:200])
        if reason := users.blocked_by_limit(user_id):
            await app.bot.send_message(chat_id, f"⛔ {reason}\nButtons und /downloads gehen weiterhin.")
            return
        users.count_request(user_id)
        typing = asyncio.create_task(keep_typing(chat_id))
        try:
            reply = await asyncio.wait_for(agent.handle(chat_id, text, user_id), RESPONSE_TIMEOUT)
        except asyncio.TimeoutError:
            log.error("chat %s: keine Antwort nach %ss", chat_id, RESPONSE_TIMEOUT)
            reply = (
                f"⚠️ Ich habe nach {RESPONSE_TIMEOUT // 60} Minuten keine Antwort von der KI bekommen. "
                "Meist ist das Kontingent kurz erschöpft. Versuch es gleich nochmal."
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Fehler bei Nachricht")
            reply = f"⚠️ Da ist intern etwas schiefgelaufen ({type(e).__name__}: {e})."
        finally:
            typing.cancel()

        if reply.strip():
            await send_formatted(chat_id, reply)
        await deliver_outbox(chat_id, user_id, ctx)

    async def deliver_outbox(chat_id: int, user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        for item in tools.take_outbox(chat_id):
            if isinstance(item, Offer):
                await send_offer(item)
            elif isinstance(item, View):
                await send_view(item, user_id)
            else:
                await show_flow(item, ctx)

    async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        users.touch_name(update.effective_user.id, _display_name(update))
        await respond(update.effective_chat.id, update.effective_user.id, update.effective_message.text, ctx)

    # ---- button flows (no AI involved) ---------------------------------------
    def markup_for(buttons: list[tuple[str, str]], prefix: str) -> InlineKeyboardMarkup | None:
        if not buttons:
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=f"{prefix}:{val}")] for lbl, val in buttons])

    async def send_offer(offer: Offer) -> None:
        buttons = [(c.label, str(i)) for i, c in enumerate(offer.cands)]
        if offer.bulk and len(offer.cands) > 1:
            buttons.append((f"⬇️ Alle {len(offer.cands)} hinzufügen", "all"))
        if offer.suggest:
            buttons.append(("🙅 Nichts davon", "none"))
        msg = await app.bot.send_message(offer.chat_id, offer.text, reply_markup=markup_for(buttons, f"o:{offer.id}"))
        offer.message_id = msg.message_id

    async def show_flow(flow: Flow, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Send or update the flow's message with its current step."""
        step = flow.step
        markup = markup_for(step.buttons, f"f:{flow.id}")
        if flow.message_id:
            try:
                await app.bot.edit_message_text(step.text, flow.chat_id, flow.message_id, reply_markup=markup)
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    raise
        else:
            msg = await app.bot.send_message(flow.chat_id, step.text, reply_markup=markup)
            flow.message_id = msg.message_id
        if step.timeout and not step.done:
            ctx.job_queue.run_once(on_flow_timeout, step.timeout, data=flow.id, name=f"f:{flow.id}")

    async def on_flow_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not allowed_user(update):
            await query.answer("Kein Zugriff.", show_alert=True)
            return
        _, flow_id, value = query.data.split(":", 2)
        for job in ctx.job_queue.get_jobs_by_name(f"f:{flow_id}"):
            job.schedule_removal()
        await query.answer()
        flow = await tools.choose(flow_id, value)
        if flow is None:
            await query.edit_message_reply_markup(None)
            return
        await show_flow(flow, ctx)

    async def on_flow_timeout(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        flow = await tools.quality_timeout(ctx.job.data)
        if flow is not None:
            await show_flow(flow, ctx)

    async def on_offer_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not allowed_user(update):
            await query.answer("Kein Zugriff.", show_alert=True)
            return
        _, offer_id, idx = query.data.split(":")
        await query.answer()
        if idx == "none":
            text = tools.reject_offer(offer_id, update.effective_user.id)
            await query.edit_message_reply_markup(None)
            await app.bot.send_message(query.message.chat_id, text or "Diese Auswahl ist abgelaufen.")
            return
        if idx == "all":
            text = await tools.add_all(offer_id)
            await query.edit_message_reply_markup(None)
            await app.bot.send_message(query.message.chat_id, text or "Diese Auswahl ist abgelaufen. Schreib mir einfach nochmal.")
            return
        res = await tools.choose_offer(offer_id, int(idx))
        if res is None:
            await query.edit_message_reply_markup(None)
            await app.bot.send_message(query.message.chat_id, "Diese Auswahl ist abgelaufen. Schreib mir einfach nochmal.")
            return
        offer, nxt = res
        if isinstance(nxt, Flow):
            if len(offer.cands) > 1 and offer.text != Offer.text:
                # Suggestions/top lists: keep the list, continue in a new message.
                nxt.message_id = None
                await query.edit_message_reply_markup(None)
            await show_flow(nxt, ctx)  # otherwise reuses the offer message
        else:
            await query.edit_message_text(nxt.text)

    async def on_notice_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Buttons under 'nothing found yet': pause 3/7 days, delete, okay."""
        query = update.callback_query
        if not allowed_user(update):
            await query.answer("Kein Zugriff.", show_alert=True)
            return
        parts = query.data.split(":")
        if parts[1] == "ok" or len(parts) != 4:
            await query.answer("👍")
            await query.edit_message_reply_markup(None)
            return
        action, service, raw_id = parts[1], parts[2], int(parts[3])
        kind = "movie" if service == "radarr" else "series"
        uid = update.effective_user.id
        try:
            if action.startswith("p") and action[1:].isdigit():
                text = await tools.pause(uid, kind, raw_id, int(action[1:]))
                await query.answer("Pausiert")
                await query.edit_message_reply_markup(None)
                await app.bot.send_message(query.message.chat_id, text)
            elif action == "rm":
                await query.answer()
                await query.edit_message_reply_markup(None)
                flow = await tools.start_remove(uid, kind, raw_id)
                await show_flow(flow, ctx)
            elif action == "w":
                await query.answer("🔔")
                await query.edit_message_reply_markup(None)
                item = await (radarr if kind == "movie" else sonarr).get(raw_id)
                ext = item.get("tmdbId") if kind == "movie" else item.get("tvdbId")
                res = await tools.run(ToolCall("n", "notify_german", {"kind": kind, "id": ext}), uid)
                if res.final:
                    await send_formatted(query.message.chat_id, res.final)
                await deliver_outbox(query.message.chat_id, uid, ctx)
            else:
                await query.answer()
        except ArrError as e:
            await query.answer(str(e)[:190], show_alert=True)

    # ---- downloads overview ---------------------------------------------------------
    def _cb_query(q: str | None) -> str:
        """The title filter rides along in the callback data (max 64 bytes in total)."""
        raw = (q or "").replace(":", " ").encode("utf-8")[:40]
        return raw.decode("utf-8", "ignore").strip()

    async def downloads_message(viewer: int, show_all: bool, finished: bool = True,
                                query: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
        sab_queue = None
        if sab:
            try:
                sab_queue = await sab.queue()
            except SabError as e:
                log.warning("%s", e)
        snap = await downloads.collect(radarr, sonarr, store, sab_queue, sab is not None, tz)
        query = _cb_query(query) or None
        text = downloads.render(snap, viewer, show_all, users.name, tz, show_finished=finished, query=query)
        # callback "d:<a|m>:<f|->[:<title filter>]": all/mine, with/without the finished list
        mode, fin, q = ("a" if show_all else "m"), ("f" if finished else "-"), (f":{query}" if query else "")
        row = [InlineKeyboardButton("🔄 Aktualisieren", callback_data=f"d:{mode}:{fin}{q}")]
        if not query:
            row.append(InlineKeyboardButton("🙈 Fertige aus" if finished else "✅ Fertige",
                                            callback_data=f"d:{mode}:{'-' if finished else 'f'}"))
        else:
            row.append(InlineKeyboardButton("📥 Alle Downloads", callback_data=f"d:{mode}:-"))
        rows = [row]
        if users.is_admin(viewer) and not query:
            rows.append([InlineKeyboardButton("👤 Nur meine" if show_all else "👥 Alle",
                                              callback_data=f"d:{'m' if show_all else 'a'}:{fin}")])
        return text, InlineKeyboardMarkup(rows)

    async def send_downloads(chat_id: int, viewer: int, finished: bool = True, query: str | None = None) -> None:
        try:
            # One title: show everybody's (the search may have been started by someone else) and its finished state.
            text, markup = await downloads_message(viewer, users.is_admin(viewer), finished or bool(query), query)
        except Exception as e:  # noqa: BLE001
            log.exception("Downloads-Übersicht fehlgeschlagen")
            await app.bot.send_message(chat_id, f"⚠️ Konnte die Downloads nicht abrufen: {e}")
            return
        await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def on_downloads(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_downloads(update.effective_chat.id, update.effective_user.id)

    async def on_downloads_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not allowed_user(update):
            await query.answer("Kein Zugriff.", show_alert=True)
            return
        viewer = update.effective_user.id
        parts = query.data.split(":", 3)
        show_all = parts[1] == "a" and users.is_admin(viewer)
        finished = len(parts) < 3 or parts[2] == "f"
        title = parts[3] if len(parts) > 3 else None
        try:
            text, markup = await downloads_message(viewer, show_all, finished, title)
        except Exception as e:  # noqa: BLE001
            await query.answer(f"Fehler: {e}"[:190], show_alert=True)
            return
        await query.answer("Aktualisiert")
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    # ---- statistics and API check (admin) ----------------------------------------------
    def stats_text() -> str:
        u = agent.usage
        return stats.render(users, store, cfg.llm.model, tz, price_known=cfg.llm.model in PRICES,
                            cache=(u.total_cached, u.total_input))

    async def send_stats(chat_id: int) -> None:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Aktualisieren", callback_data="s:r")]])
        await app.bot.send_message(chat_id, stats_text(), parse_mode=ParseMode.HTML, reply_markup=kb)

    async def on_stats(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_stats(update.effective_chat.id)

    async def on_stats_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not (update.effective_user and users.is_admin(update.effective_user.id)):
            await query.answer("Nur für Admins.", show_alert=True)
            return
        await query.answer("Aktualisiert")
        try:
            await query.edit_message_text(stats_text(), parse_mode=ParseMode.HTML, reply_markup=query.message.reply_markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def check_text() -> str:
        checks = await health.run_checks(bot=app.bot, provider=agent.provider, model=cfg.llm.model,
                                         radarr=radarr, sonarr=sonarr, sab=sab, tmdb=tmdb)
        return health.render(checks)

    async def send_check(chat_id: int) -> None:
        msg = await app.bot.send_message(chat_id, "🩺 Prüfe alle APIs …")
        await app.bot.edit_message_text(await check_text(), chat_id, msg.message_id, parse_mode=ParseMode.HTML)

    async def on_check(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_check(update.effective_chat.id)

    async def send_view(view: View, viewer: int) -> None:
        if view.kind == "downloads":
            await send_downloads(view.chat_id, viewer, view.finished, view.query)
        elif view.kind == "stats" and users.is_admin(viewer):
            await send_stats(view.chat_id)
        elif view.kind == "users" and users.is_admin(viewer):
            await app.bot.send_message(view.chat_id, users_text(), parse_mode=ParseMode.HTML, reply_markup=users_markup())
        elif view.kind == "check" and users.is_admin(viewer):
            await send_check(view.chat_id)
        elif view.kind == "profile":
            await send_profile(view.chat_id, viewer)
        elif view.kind == "calendar":
            days, mine = view.arg or (7, False)
            await send_calendar(view.chat_id, viewer, days, mine)
        elif view.kind == "disk":
            await send_disk(view.chat_id)
        elif view.kind == "plex":
            await send_plex(view.chat_id, view.arg or {})
        elif view.kind == "plex_link":
            await start_plex_link(view.chat_id, viewer)
        elif view.kind == "media" and users.is_admin(viewer):
            section, days = view.arg or ("overview", None)
            if days:
                media_state["days"] = int(days)
            await send_media(view.chat_id, section or "overview")

    # ---- media check (admin) -------------------------------------------------------------------
    media_state: dict = {"report": None, "days": cfg.unwatched_days}

    async def media_report(fresh: bool = False) -> media.Report:
        r = media_state["report"]
        if fresh or r is None or (datetime.now(timezone.utc) - r.made) > timedelta(minutes=10):
            accounts = [a for a in plex_store.all() if users.is_allowed(a.user_id)]
            r = media_state["report"] = await media.collect(radarr, sonarr, plex_client, accounts)
        return r

    def media_view(r: media.Report, section: str) -> tuple[str, InlineKeyboardMarkup]:
        days = media_state["days"]
        if section in media.SECTIONS:
            text, buttons = media.render_section(r, section, days, users.name)
        else:
            text, buttons = media.render_overview(r, days, users.name)
        rows = [[InlineKeyboardButton(label, callback_data=data)] for label, data in buttons]
        return text, InlineKeyboardMarkup(rows)

    async def send_media(chat_id: int, section: str = "overview") -> None:
        msg = await app.bot.send_message(chat_id, "🗂 Prüfe die Mediathek …")
        try:
            r = await media_report()
        except Exception as e:  # noqa: BLE001
            log.exception("Mediathek-Check fehlgeschlagen")
            await msg.edit_text(f"⚠️ Mediathek-Check fehlgeschlagen: {e}")
            return
        text, markup = media_view(r, section)
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def on_media(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_media(update.effective_chat.id)

    async def on_media_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not (update.effective_user and users.is_admin(update.effective_user.id)):
            await query.answer("Nur für Admins.", show_alert=True)
            return
        parts = query.data.split(":")
        action = parts[1]
        if action == "gf" and len(parts) == 4:  # check/replace the language of one title
            await query.answer("Prüfe die Tonspur …")
            uid = update.effective_user.id
            arg = {"tmdb_id": int(parts[3])} if parts[2] == "m" else {"tvdb_id": int(parts[3])}
            res = await tools.run(ToolCall("m", "german_fix", arg), uid)
            if res.final:
                await send_formatted(query.message.chat_id, res.final)
            elif res.is_error:
                await app.bot.send_message(query.message.chat_id, f"⚠️ {res.content}")
            await deliver_outbox(query.message.chat_id, uid, ctx)
            return
        if action == "search":
            started = []
            for svc, cmd in ((radarr, "MissingMoviesSearch"), (sonarr, "MissingEpisodeSearch")):
                if svc:
                    try:
                        await svc.command(cmd)
                        started.append(svc.kind)
                    except ArrError as e:
                        log.warning("%s: %s", cmd, e)
            await query.answer(("🔍 Suche gestartet in " + " und ".join(started)) if started else "Hat nicht geklappt",
                               show_alert=True)
            return
        if action == "scan":
            acc = plex_store.get(update.effective_user.id) or next(iter(plex_store.all()), None)
            try:
                n = await plex_client.refresh_libraries(acc) if acc else 0
            except plex.PlexError as e:
                await query.answer(str(e)[:190], show_alert=True)
                return
            await query.answer(f"🔄 {n} Plex-Bibliotheken werden gescannt. In ein paar Minuten „Neu prüfen“.",
                               show_alert=True)
            return
        await query.answer("Prüfe …" if action == "r" else None)
        try:
            r = await media_report(fresh=action == "r")
        except Exception as e:  # noqa: BLE001
            await app.bot.send_message(query.message.chat_id, f"⚠️ Mediathek-Check fehlgeschlagen: {e}")
            return
        text, markup = media_view(r, "overview" if action in ("o", "r") else action)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    # ---- calendar and disk space -------------------------------------------------------------
    async def send_calendar(chat_id: int, viewer: int, days: int = 7, mine: bool = False) -> None:
        today = date.today()
        only = None
        if mine:
            only = {(r["kind"], r.get("tmdb") if r["kind"] == "movie" else r.get("tvdb"))
                    for r in store.state.requests if r["chat_id"] == viewer}
        entries = await extras.calendar_entries(radarr, sonarr, today, today + timedelta(days=days - 1), tz, only)
        text = extras.render_calendar(entries, today, today + timedelta(days=days - 1), today, mine)
        await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    async def cleanup_candidates() -> list[str]:
        """Movies everybody with a linked Plex has watched, biggest first."""
        accounts = plex_store.all()
        if not accounts or not radarr:
            return []
        seen = None
        for a in accounts:
            mine = {tmdb for kind, tmdb, _ in profile_db.known(a.user_id, "watched") if kind == "movie" and tmdb}
            seen = mine if seen is None else seen & mine
        try:
            movies = [m for m in await radarr.library() if m.get("tmdbId") in (seen or set()) and m.get("sizeOnDisk")]
        except ArrError:
            return []
        movies.sort(key=lambda m: -(m.get("sizeOnDisk") or 0))
        return [f"• {m.get('title')} ({m.get('year')}) · {extras._size(m['sizeOnDisk'])}" for m in movies[:5]]

    async def send_disk(chat_id: int) -> None:
        ds = await extras.disks(radarr, sonarr)
        low = extras.low_disks(ds, cfg.disk_warn_gb)
        text = extras.render_disks(ds, cfg.disk_warn_gb, await cleanup_candidates() if low or ds else None)
        await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    async def on_calendar(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_calendar(update.effective_chat.id, update.effective_user.id)

    async def on_disk(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_disk(update.effective_chat.id)

    # ---- Plex ------------------------------------------------------------------------------
    async def send_plex(chat_id: int, arg: dict) -> None:
        markup = None
        if arg.get("url"):
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ In Plex öffnen", url=arg["url"])]])
        if arg.get("html"):
            await app.bot.send_message(chat_id, arg["html"], parse_mode=ParseMode.HTML, reply_markup=markup)
        else:
            await app.bot.send_message(chat_id, arg.get("text") or "", reply_markup=markup)

    def plex_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Jetzt abgleichen", callback_data="x:s"),
                                      InlineKeyboardButton("🔌 Trennen", callback_data="x:u")]])

    def plex_status_text(a: plex.Account) -> str:
        when = f", zuletzt abgeglichen {a.synced[:16].replace('T', ' ')} UTC" if a.synced else ""
        return (f"✅ Plex verbunden als <b>{html.escape(a.username)}</b> mit „{html.escape(a.server_name)}“{when}.\n"
                "Ich lerne, was du schaust, und beantworte „Wo war ich bei …?“.")

    async def plex_sync(a: plex.Account) -> int:
        try:
            n = await plex.sync(plex_client, plex_store, profile_db, a)
            log.info("Plex-Abgleich %s: %d neu gesehen", a.username, n)
            return n
        except plex.PlexError as e:
            log.warning("Plex-Abgleich %s: %s", a.username, e)
            raise

    async def start_plex_link(chat_id: int, uid: int) -> None:
        acc = plex_store.get(uid)
        if acc:
            await app.bot.send_message(chat_id, plex_status_text(acc), parse_mode=ParseMode.HTML,
                                       reply_markup=plex_markup())
            return
        try:
            pin_id, code = await plex_client.create_pin()
        except plex.PlexError as e:
            await app.bot.send_message(chat_id, f"⚠️ {e}")
            return
        msg = await app.bot.send_message(
            chat_id,
            f"🔗 <b>Plex verbinden</b>\n\n1. Öffne <b>plex.tv/link</b> (Button unten) und melde dich an\n"
            f"2. Gib diesen Code ein: <code>{html.escape(code)}</code>\n\n"
            f"Der Code gilt {plex.PIN_MINUTES} Minuten. Dein Passwort bekomme ich dabei nicht zu sehen.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 plex.tv/link öffnen", url=plex.LINK_URL)]]))

        async def finish() -> None:
            try:
                token = await plex.wait_for_pin(plex_client, pin_id)
                if not token:
                    await msg.edit_text("⌛ Der Code ist abgelaufen. Schreib /plex für einen neuen.")
                    return
                admin = plex_store.get(cfg.admin_id)
                url, stoken, mid, name = await plex_client.pick_server(token, admin.machine_id if admin else None)
                acc = plex.Account(uid, token, await plex_client.username(token), url, stoken, mid, name,
                                   plex.utcnow())
                plex_store.save(acc)
                await msg.edit_text(plex_status_text(acc) + "\n\n⏳ Ich gleiche jetzt ab, was du schon gesehen hast …",
                                    parse_mode=ParseMode.HTML)
                n = await plex_sync(acc)
                await app.bot.send_message(chat_id, f"👍 Abgleich fertig: {n} gesehene Titel übernommen. "
                                                    "Deine Vorschläge berücksichtigen das ab jetzt.")
            except plex.PlexError as e:
                await app.bot.send_message(chat_id, f"⚠️ Plex: {e}")
            except Exception:  # noqa: BLE001
                log.exception("Plex verbinden fehlgeschlagen")
                await app.bot.send_message(chat_id, "⚠️ Plex verbinden hat nicht geklappt. Versuch es nochmal: /plex")

        task = asyncio.create_task(finish())
        background.add(task)
        task.add_done_callback(background.discard)

    async def on_plex(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await start_plex_link(update.effective_chat.id, update.effective_user.id)

    async def on_plex_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not allowed_user(update):
            await query.answer("Kein Zugriff.", show_alert=True)
            return
        uid, action = update.effective_user.id, query.data.split(":")[1]
        acc = plex_store.get(uid)
        if not acc:
            await query.answer("Nicht verbunden.")
            await query.edit_message_reply_markup(None)
            return
        if action == "u":
            plex_store.remove(uid)
            await query.answer("Getrennt")
            await query.edit_message_text("🔌 Plex getrennt. Was du bisher geschaut hast, bleibt in deinem Profil "
                                          "(löschen per /profil).")
            return
        await query.answer("Gleiche ab …")
        try:
            n = await plex_sync(acc)
        except plex.PlexError as e:
            await app.bot.send_message(query.message.chat_id, f"⚠️ Plex: {e}")
            return
        await app.bot.send_message(query.message.chat_id, f"🔄 Abgleich fertig: {n} neue gesehene Titel.")

    # ---- daily / hourly jobs -------------------------------------------------------------------
    async def watch_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        for n in await watch.check_watches(tools):
            try:
                if n.text:
                    await send_formatted(n.chat_id, n.text)
                await deliver_outbox(n.chat_id, n.chat_id, ctx)
            except (BadRequest, Forbidden) as e:
                log.info("Deutsch-Meldung an %s nicht zugestellt: %s", n.chat_id, e)

    async def digest_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        for uid in profile_db.digest_users(date.today().weekday()):
            if not users.is_allowed(uid):
                continue
            try:
                d = await watch.weekly_digest(tools, uid, tz)
                await app.bot.send_message(uid, d.text, parse_mode=ParseMode.HTML)
                if d.suggestions:
                    await tools.run(ToolCall("d", "suggest", {}), uid)
                    await deliver_outbox(uid, uid, ctx)
            except (BadRequest, Forbidden) as e:
                log.info("Wochenrückblick an %s nicht zugestellt: %s", uid, e)
            except Exception:  # noqa: BLE001
                log.exception("Wochenrückblick für %s fehlgeschlagen", uid)

    disk_warned: set[str] = set()

    async def disk_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        ds = await extras.disks(radarr, sonarr)
        low = {d.path for d in extras.low_disks(ds, cfg.disk_warn_gb)}
        new = low - disk_warned
        if new:
            text = extras.render_disks([d for d in ds if d.path in new], cfg.disk_warn_gb, await cleanup_candidates())
            await tell_admins("⚠️ Wenig Speicherplatz!\n\n" + text, html_text=True)
        disk_warned.intersection_update(low)  # warn again after it was fine in between
        disk_warned.update(low)

    async def plex_sync_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        for a in plex_store.all():
            try:
                await plex_sync(a)
            except plex.PlexError:
                pass

    # ---- user profile ---------------------------------------------------------------------
    def profile_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Alles vergessen", callback_data="p:x")]])

    async def send_profile(chat_id: int, uid: int) -> None:
        text = profiles.render(profile_db.profile(uid), html.escape(users.name(uid)))
        await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=profile_markup())

    async def on_profile(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_profile(update.effective_chat.id, update.effective_user.id)

    async def on_profile_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not allowed_user(update):
            await query.answer("Kein Zugriff.", show_alert=True)
            return
        uid, action = update.effective_user.id, query.data.split(":")[1]
        await query.answer()
        if action == "x":  # ask first
            await query.edit_message_reply_markup(InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Ja, alles löschen", callback_data="p:y"),
                InlineKeyboardButton("Abbrechen", callback_data="p:n")]]))
        elif action == "y":
            profile_db.forget_all(uid)
            await query.edit_message_text("🗑 Erledigt – ich habe alles über deinen Geschmack vergessen. "
                                          "Ab jetzt lerne ich neu.")
        else:
            await query.edit_message_reply_markup(profile_markup())

    # ---- access requests and user management ----------------------------------------
    async def on_unknown(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not update.effective_message:
            return
        role = users.role(user.id)
        if role == BLOCKED:
            return  # silently ignored
        if role == PENDING:
            await update.effective_message.reply_text("⏳ Deine Anfrage liegt beim Admin. Du bekommst eine Nachricht, sobald er entschieden hat.")
            return
        name = _display_name(update)
        if users.request(user.id, name):
            log.info("Zugriffsanfrage von %s (%s)", user.id, name)
            uname = f" (@{user.username})" if user.username else ""
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Freigeben", callback_data=f"u:ok:{user.id}"),
                InlineKeyboardButton("❌ Ablehnen", callback_data=f"u:no:{user.id}"),
            ]])
            await tell_admins(f"🙋 {name}{uname} möchte den Bot nutzen.\nTelegram-ID: {user.id}", kb)
        await update.effective_message.reply_text(
            "👋 Hallo! Ich habe den Admin gefragt, ob du mich nutzen darfst. Du bekommst Bescheid."
        )

    def users_text() -> str:
        lines = ["<b>👥 Nutzer</b>"]
        for u in users.listing():
            u.usage.roll()
            lim = users.limits_for(u.id)
            lim_txt = ", ".join(limit_text(k, v) for k, v in lim.items()) or "keine Limits"
            cost = f"{u.usage.month_cost:.3f} $".replace(".", ",")
            lines.append(
                f"\n{ROLE_LABEL.get(u.role, u.role)} <b>{html.escape(u.name)}</b> <code>{u.id}</code>\n"
                f"   heute {u.usage.day_requests} · Monat {u.usage.month_requests} Anfragen · {cost}\n"
                f"   <i>{html.escape(lim_txt)}</i>"
            )
        std = ", ".join(limit_text(k, v) for k, v in users.defaults.items()) or "keine"
        tot = ", ".join(limit_text(k, v) for k, v in users.total_limits.items()) or "keins"
        users.total.roll()
        lines.append(f"\n<b>Standard-Limits</b> für Nutzer: {html.escape(std)}")
        lines.append(f"<b>Gesamtbudget</b>: {html.escape(tot)} · diesen Monat "
                     + f"{users.total.month_cost:.3f} $".replace(".", ","))
        lines.append("\nÄndern mit /limit, z. B. <code>/limit standard tag 10</code>")
        return "\n".join(lines)[:4096]

    def users_markup() -> InlineKeyboardMarkup | None:
        rows = []
        for u in users.listing():
            if u.role == ADMIN:
                continue
            short = u.name[:18]
            if u.role in (PENDING, BLOCKED):
                rows.append([InlineKeyboardButton(f"✅ {short} freigeben", callback_data=f"u:ok:{u.id}"),
                             InlineKeyboardButton("🗑", callback_data=f"u:rm:{u.id}")])
            else:
                rows.append([InlineKeyboardButton(f"🚫 {short} sperren", callback_data=f"u:no:{u.id}"),
                             InlineKeyboardButton("🗑", callback_data=f"u:rm:{u.id}")])
        return InlineKeyboardMarkup(rows) if rows else None

    async def on_users(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await app.bot.send_message(update.effective_chat.id, users_text(), parse_mode=ParseMode.HTML,
                                   reply_markup=users_markup())

    async def on_user_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not (update.effective_user and users.is_admin(update.effective_user.id)):
            await query.answer("Nur für Admins.", show_alert=True)
            return
        _, action, raw_uid = query.data.split(":")
        uid = int(raw_uid)
        name = users.name(uid)
        try:
            if action == "ok":
                users.set_role(uid, USER)
                note = f"✅ {name} ist freigegeben."
                try:
                    await app.bot.send_message(uid, "🎉 Du bist freigeschaltet!\n\n" + HELP)
                except (BadRequest, Forbidden):
                    pass
            elif action == "no":
                users.set_role(uid, BLOCKED)
                note = f"🚫 {name} ist gesperrt."
            elif action == "rm":
                users.remove(uid)
                note = f"🗑 {name} wurde entfernt (kann neu anfragen)."
            else:
                await query.answer()
                return
        except ValueError as e:
            await query.answer(str(e), show_alert=True)
            return
        await query.answer(note)
        try:
            if query.message and query.message.text and query.message.text.startswith("👥"):
                await query.edit_message_text(users_text(), parse_mode=ParseMode.HTML, reply_markup=users_markup())
            else:
                await query.edit_message_text(f"{query.message.text}\n\n{note}" if query.message else note)
        except BadRequest:
            pass

    async def on_limit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        usage = (
            "So geht's: <code>/limit WER ART WERT</code>\n"
            "WER: Name, Telegram-ID, <code>standard</code> (alle Nutzer) oder <code>gesamt</code> (ganzer Bot)\n"
            f"ART: {', '.join(f'<code>{k}</code> ({v})' for k, v in LIMIT_KEYS.items())}\n"
            "WERT: Zahl oder <code>aus</code>"
        )
        if len(args) != 3:
            await app.bot.send_message(update.effective_chat.id, usage, parse_mode=ParseMode.HTML)
            return
        try:
            await update.effective_message.reply_text(users.apply_limit(args[0], args[1], args[2]))
        except ValueError as e:
            await update.effective_message.reply_text(f"⚠️ {e}")

    # ---- commands -----------------------------------------------------------
    def help_for(uid: int) -> tuple[str, str | None]:
        text = html.escape(HELP)
        if users.is_admin(uid):
            text += ADMIN_HELP
        return text, ParseMode.HTML

    async def on_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        users.touch_name(update.effective_user.id, _display_name(update))
        text, mode = help_for(update.effective_user.id)
        await update.effective_message.reply_text("Hallo! 🎬\n\n" + text, parse_mode=mode)

    async def on_reset(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        agent.reset(update.effective_chat.id)
        await update.effective_message.reply_text("Okay, neues Gespräch.")

    async def on_usage(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        model = cfg.llm.model

        def n(x: int) -> str:
            return f"{x:,}".replace(",", ".")

        def money(x: float) -> str:
            return f"{x:.4f} $".replace(".", ",")

        u = users.users.get(uid)
        lines = []
        if u:
            u.usage.roll()
            lim = users.limits_for(uid)
            lines.append("<b>📊 Dein Verbrauch</b>")
            for key, label, val in (
                ("tag", "Anfragen heute", u.usage.day_requests),
                ("monat", "Anfragen im Monat", u.usage.month_requests),
                ("tokens", "Tokens im Monat", u.usage.month_tokens),
                ("kosten", "Kosten im Monat", u.usage.month_cost),
            ):
                shown = money(val) if key == "kosten" else n(int(val))
                if key in lim:
                    limit = money(lim[key]) if key == "kosten" else n(int(lim[key]))
                    pct = min(100, 100 * val / lim[key]) if lim[key] else 100
                    lines.append(f"{label}: {shown} von {limit}\n<code>{downloads.bar(pct)}</code> {pct:.0f} %")
                else:
                    lines.append(f"{label}: {shown}")
        if users.is_admin(uid):
            s = agent.usage
            cost_day = s.cost(model, s.input, s.cached, s.output)
            cost_all = s.cost(model, s.total_input, s.total_cached, s.total_output)
            users.total.roll()
            lines += [
                f"\n<b>🤖 Gesamt ({html.escape(model)})</b>",
                f"Heute: {s.requests} Anfragen, {s.calls} KI-Aufrufe, {n(s.input)} Tokens rein "
                f"({n(s.cached)} Cache), {n(s.output)} raus" + (f", ≈ {money(cost_day)}" if cost_day is not None else ""),
                f"Seit Start: {s.total_requests} Anfragen" + (f", ≈ {money(cost_all)}" if cost_all is not None else ""),
                f"Diesen Monat (alle Nutzer): {users.total.month_requests} Anfragen, ≈ {money(users.total.month_cost)}",
            ]
            if s.cost(model, 0, 0, 0) is None:
                lines.append("<i>Für dieses Modell ist kein Preis hinterlegt – Kostenlimits greifen nicht, Token-Limits schon.</i>")
        await app.bot.send_message(update.effective_chat.id, "\n".join(lines) or "Noch nichts verbraucht.",
                                   parse_mode=ParseMode.HTML)

    # ---- misc -------------------------------------------------------------
    async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unbehandelter Fehler", exc_info=ctx.error)
        if isinstance(update, Update) and update.effective_chat and allowed_user(update):
            try:
                await app.bot.send_message(
                    update.effective_chat.id, f"⚠️ Fehler: {type(ctx.error).__name__}: {ctx.error}"[:500]
                )
            except Exception:  # noqa: BLE001
                pass

    async def poll_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        await tracker.poll()

    async def post_init(application: Application) -> None:
        base = [
            BotCommand("downloads", "Deine Downloads"),
            BotCommand("neu", "Gespräch zurücksetzen"),
            BotCommand("verbrauch", "Anfragen und Limits"),
            BotCommand("profil", "Was ich über dich weiß"),
            BotCommand("kalender", "Was kommt diese Woche"),
            BotCommand("plex", "Plex verbinden"),
            BotCommand("hilfe", "Was kann ich?"),
        ]
        await application.bot.set_my_commands(base, scope=BotCommandScopeDefault())
        for admin in users.admins():
            try:
                await application.bot.set_my_commands(
                    base + [BotCommand("nutzer", "Nutzer verwalten"), BotCommand("statistik", "Statistik"),
                            BotCommand("check", "APIs prüfen"), BotCommand("limit", "Limits setzen"),
                            BotCommand("mediathek", "Mediathek prüfen und aufräumen")],
                    scope=BotCommandScopeChat(admin),
                )
            except BadRequest as e:  # admin never wrote to the bot yet
                log.info("Admin-Befehle für %s nicht gesetzt: %s", admin, e)
        application.job_queue.run_repeating(poll_job, interval=cfg.poll_interval, first=5)
        application.job_queue.run_daily(watch_job, time(9, 0, tzinfo=tz), name="deutsch")
        application.job_queue.run_daily(digest_job, time(10, 0, tzinfo=tz), name="wochenrueckblick")
        application.job_queue.run_repeating(plex_sync_job, interval=6 * 3600, first=600, name="plex")
        if cfg.disk_warn_gb > 0:
            application.job_queue.run_repeating(disk_job, interval=3600, first=120, name="speicher")
        try:
            n = await tools.import_history()
            if n:
                log.info("Nutzerprofile: %d frühere Anfragen übernommen", n)
        except Exception:  # noqa: BLE001 - never block the start
            log.exception("Nutzerprofile: Import der Anfragen fehlgeschlagen")
        extras = [x for x, on in (("SABnzbd", sab), ("TMDB", tmdb)) if on]
        log.info("Bot läuft mit %s / %s%s, Admin %s", cfg.llm.provider, cfg.llm.model,
                 f" + {', '.join(extras)}" if extras else "", cfg.admin_id)

    async def post_shutdown(_: Application) -> None:
        for svc in (radarr, sonarr, sab, tmdb):
            if svc:
                await svc.close()
        await plex_client.close()
        plex_store.close()
        profile_db.close()

    app = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(True)  # a slow answer must not block buttons or other chats
        .build()
    )
    app.add_handler(CommandHandler(["start", "hilfe", "help"], on_start, filters=allowed))
    app.add_handler(CommandHandler(["neu", "reset"], on_reset, filters=allowed))
    app.add_handler(CommandHandler(["downloads", "status", "dl"], on_downloads, filters=allowed))
    app.add_handler(CommandHandler("verbrauch", on_usage, filters=allowed))
    app.add_handler(CommandHandler(["profil", "profile", "ich"], on_profile, filters=allowed))
    app.add_handler(CommandHandler(["kalender", "woche"], on_calendar, filters=allowed))
    app.add_handler(CommandHandler(["speicher", "platz"], on_disk, filters=allowed))
    app.add_handler(CommandHandler("plex", on_plex, filters=allowed))
    app.add_handler(CommandHandler("nutzer", on_users, filters=is_admin))
    app.add_handler(CommandHandler("limit", on_limit, filters=is_admin))
    app.add_handler(CommandHandler(["statistik", "stats"], on_stats, filters=is_admin))
    app.add_handler(CommandHandler(["check", "apis"], on_check, filters=is_admin))
    app.add_handler(CommandHandler(["mediathek", "medien", "aufraeumen"], on_media, filters=is_admin))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_flow_button, pattern=r"^f:"))
    app.add_handler(CallbackQueryHandler(on_offer_button, pattern=r"^o:"))
    app.add_handler(CallbackQueryHandler(on_downloads_button, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(on_user_button, pattern=r"^u:"))
    app.add_handler(CallbackQueryHandler(on_notice_button, pattern=r"^n:"))
    app.add_handler(CallbackQueryHandler(on_stats_button, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(on_profile_button, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(on_plex_button, pattern=r"^x:"))
    app.add_handler(CallbackQueryHandler(on_media_button, pattern=r"^m:"))
    app.add_handler(MessageHandler(~allowed, on_unknown))
    app.add_handler(MessageHandler(allowed & filters.COMMAND, on_start))  # unknown command: show help
    app.add_error_handler(on_error)
    return app
