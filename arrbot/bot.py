"""Telegram front end."""

from __future__ import annotations

import asyncio
import dataclasses
import html
import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
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

from . import arrhealth, debuglog, diagnose, downloads, fmt, notify
from .agent import Agent
from .arr import ArrError, Radarr, Sonarr
from .config import Config
from .llm import ToolCall, make_provider
from .sabnzbd import SABnzbd, SabError
from .state import Store, Tracked, utcnow
from .tmdb import TMDB
from .tools import Candidate, Flow, Offer, ToolRunner, View, mute, release_languages
from .tracker import Tracker
from . import activity, approval, cleanup, guard, monthly, recipes, sitewatch, wishlist, extras, german, health, listen, media, plex, profiles, stats, version, watch
from . import admin as admin_view
from . import busy, langpick, qprofiles
from .profiles import ProfileStore
from .agent import PRICES, UsageStats
from .users import ADMIN, BLOCKED, PENDING, USER, UserStore, limit_text, quality_label

log = logging.getLogger(__name__)

HELP = (
    "Schreib mir einfach, was du sehen willst, z. B.:\n"
    "• „Lad mir Dune Teil 2 runter“\n"
    "• „Staffel 1 und 2 von Dragonball Z“\n"
    "• „Was soll ich als Nächstes schauen?“\n"
    "• „Welche Filme waren die Top 5 2024?“\n"
    "• „Lade die Top 3 Filme vom Oktober 2025“\n"
    "• „Gibt es Severance schon auf Deutsch?“\n"
    "• „Merk dir, ich mag keine Horrorfilme“ / „Was weißt du über mich?“\n"
    "• „Sag mir, wenn Dune 3 auf Deutsch kommt“ / „Wo war ich bei Dark?“\n"
    "• „Merk dir Avatar 4“ – ich lege es an, sobald es einen Termin gibt\n"
    "• „Was können Anna und ich zusammen schauen?“\n"
    "• „Was Lustiges unter 90 Minuten“ / „Wie Dark, aber lustiger“ / „Inception fand ich super“\n"
    "• „Schick mir jeden Sonntag einen Wochenrückblick“\n"
    "• einen Link von IMDb, TMDB, Trakt, Letterboxd, JustWatch …\n\n"
    "Ich sage Bescheid, wenn ein Download startet, wann er fertig ist und ob es Probleme gibt – "
    "was davon, stellst du unter /meldungen ein („nur wenn fertig“ geht auch per Text).\n\n"
    "/downloads – deine Downloads mit Fortschritt\n"
    "/verbrauch – deine Anfragen und Limits\n"
    "/profil – was ich über deinen Geschmack weiß\n"
    "/kalender – was diese Woche kommt\n"
    "/plex – Plex verbinden (gesehen, weiterschauen)\n"
    "/neu – Gespräch zurücksetzen\n"
    "/stumm 30 – Download-Meldungen 30 Min. stumm (auch 2h, 3d; „nerv mich 3 Tage nicht“), /laut – wieder an\n"
    "/meldungen – welche Meldungen du bekommst (Start, fertig, Probleme, nichts gefunden)\n"
    "/version – welche Version und welche KI läuft („Welche Version hast du?“ geht auch)\n\n"
    "Du kannst Filme und Serien suchen und hinzufügen. Löschen, Pausieren oder Austauschen macht der Admin.\n\n"
    "Hinweis: Der Admin sieht, was du fragst und ob es geklappt hat – so kann er Probleme beheben."
)
ADMIN_HELP = (
    "\n\n<b>Admin</b>\n"
    "/admin – Übersicht: Version, Modelle, Nutzer, Kosten, offene Freigaben, Probleme – mit Buttons zu allem\n"
    "/nutzer – Nutzer freigeben, sperren, entfernen\n"
    "/statistik – Nutzer, Anfragen, Tokens, Kosten\n"
    "/mediathek – was fehlt, was nicht in Plex ist, was gesehen oder lange ungesehen ist (aufräumen)\n"
    "/freigaben – Wünsche erst freigeben, z. B. „Wünsche von Anna erst freigeben, wenn es Horrorfilme sind“\n"
    "/regeln – Aufräumregeln, z. B. „Lösch Filme, die alle gesehen haben, nach 30 Tagen“\n"
    "🗂 Aufräumen auf Befehl: „Lösche alle Filme, die ich gesehen habe“, „Filme über 180 Tage, nie geschaut“ – "
    "erst Liste, dann Bestätigung; „Was frisst am meisten Platz?“, „Dune nie löschen“, „Such doppelte Filme“\n"
    "/version – Versionsnummer, ob es ein Update gibt und was neu ist\n"
    "/aktivitaet – was Nutzer fragen, was geklappt hat und wo es Probleme gab (<code>/aktivitaet Anna</code>)\n"
    "/check – alle APIs prüfen\n"
    "/log – was ich bei der letzten Nachricht gemacht habe: KI-Schritte, Werkzeuge, jede Anfrage an "
    "Sonarr/Radarr, Suchergebnis (<code>/log 3</code>, <code>/log Anna</code>)\n"
    "/debug an – das Log kommt nach jeder Antwort mit, Suchergebnisse sobald sie fertig sind\n"
    "/rezepte – Werkzeuge, die ich mir selbst gebaut habe\n"
    "Plex-Sammlungen: „Mach eine Sammlung Marvel“, „Automatische Sammlungen pro Nutzer an“\n"
    "/monatsbericht – Kosten, Nutzer, Erfolgsquote, Sperren (kommt am 1. von selbst; "
    "<code>/monatsbericht jetzt</code> für den laufenden Monat)\n"
    "🚨 Spam-Schutz: wer flutet, wird vorsorglich gesperrt – du bekommst eine Meldung mit „Entsperren“. "
    "Bei Übertreibung (viele Titel, Limit fast voll, weit mehr als die anderen) warne ich dich.\n"
    "Nur für dich: „Lösch The Office“, „Such in 3 Tagen nochmal nach Dune 3“, „Prüf ob Dark auf Deutsch ist und "
    "tausch es aus“, „Überwache https://… nach neuen Filmen“, „Anna darf höchstens 1080p“ "
    "(„Standard höchstens 1080p“ gilt für alle).\n"
    "Neue Art Frage, für die mir ein Werkzeug fehlt? Ich biete an, mir eins zu bauen, und sage vorher, was es kostet.\n"
    "/limit – Limits setzen, z. B.\n"
    "<code>/limit standard tag 10</code>\n"
    "<code>/limit Anna monat 50</code>\n"
    "<code>/limit gesamt kosten 1</code>\n"
    "<code>/limit 123456 tokens aus</code>\n"
    "<code>/limit Sarah filme_tag 3</code> · <code>/limit standard folgen_woche 40</code> (Titel pro "
    "Stunde/Tag/Woche/Monat)\n"
    "<code>/limit gesamt tokens_tag 200000</code> (Tokens pro Tag, ganzer Bot) · "
    "<code>/limit empfehlung</code> (ich schlage Limits aus dem Verbrauch vor)\n"
    "Rechte: „Sarah darf ihre Filme und Serien löschen“, „Was darf Sarah?“, „Sarah darf nicht mehr löschen“\n\n"
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
    turn_tokens: dict[int, int] = {}  # tokens of the message being answered (activity log)

    def on_usage(uid: int, tokens: int, cost: float | None) -> None:
        users.add_usage(uid, tokens, cost)
        turn_tokens[uid] = turn_tokens.get(uid, 0) + tokens
        for warning in users.total_warnings():  # 80 % / 100 % of a total limit: tell the admins once
            task = asyncio.get_running_loop().create_task(
                tell_admins(warning + "\nÄndern: /limit oder „Empfiehl mir ein Limit“."))
            background.add(task)
            task.add_done_callback(background.discard)

    agent.on_usage = on_usage
    activity_db = activity.ActivityStore(cfg.data_dir)
    german.confirmed.load(cfg.data_dir)
    rules = cleanup.RuleStore(cfg.data_dir)
    tools.cleanup = rules
    approvals = approval.ApprovalStore(cfg.data_dir)
    tools.approvals = approvals
    sites = sitewatch.SiteStore(cfg.data_dir)
    wishes = wishlist.WishStore(cfg.data_dir)
    tools.wishes = wishes
    tools.recipes = recipes.RecipeStore(cfg.data_dir)
    try:
        tools.builder = make_provider(dataclasses.replace(cfg.llm, model=cfg.recipe_model,
                                                          reasoning=cfg.recipe_reasoning, max_output=3000))
        tools.builder_model = cfg.recipe_model
    except Exception as e:  # noqa: BLE001 - the bot works without building tools
        log.warning("Werkzeug-Modell %s nicht nutzbar: %s", cfg.recipe_model, e)

    def builder_cost(u: dict) -> float | None:
        return UsageStats.cost(cfg.recipe_model, u.get("input", 0) or 0, u.get("cached", 0) or 0,
                               u.get("output", 0) or 0)

    def on_llm_usage(uid: int, u: dict, cost: float | None) -> None:
        on_usage(uid, (u.get("input", 0) or 0) + (u.get("output", 0) or 0), cost)

    tools.builder_cost = builder_cost
    tools.on_llm_usage = on_llm_usage
    tools.sites, tools.site_hours = sites, cfg.site_hours
    listener = listen.Listener(cfg.listen_api_key, cfg.media_paths)
    answering: set[int] = set()  # chats in an AI turn: their additions are already logged with the question
    busy_with: dict[int, list[tuple[str, float]]] = {}  # chat -> [(question, since)]: "arbeitest du noch?"
    profile_db = ProfileStore(cfg.data_dir)
    tools.profiles = profile_db
    plex_store = plex.PlexStore(cfg.data_dir)
    plex_client = plex.Plex(plex_store.client_id(), cfg.plex_url)
    tools.plex, tools.plex_store = plex_client, plex_store

    def on_user_removed(uid: int) -> None:
        profile_db.forget_all(uid)
        plex_store.remove(uid)
        activity_db.forget(uid)
        approvals.forget(uid)
        sites.forget(uid)
        wishes.forget(uid)
        shield.forget(uid)

    users.on_remove = on_user_removed
    shield = guard.Guard(cfg.data_dir, cfg.spam_check)

    def on_role(uid: int, old: str | None, new: str) -> None:
        shield.forget(uid)  # a fresh start after unblocking
        if old == BLOCKED and new == USER:
            shield.log(uid, guard.UNBLOCK, "vom Admin freigegeben")

    users.on_role = on_role
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
        if chat_id not in answering:
            activity_db.record(chat_id, text, [], activity.ACTION)
        if not users.is_admin(chat_id):
            await tell_admins(f"📣 {users.name(chat_id)}: {text}")
            if text.startswith("➕") and (v := shield.added(chat_id)):
                await guard_verdict(chat_id, v)

    async def guard_verdict(uid: int, v: guard.Verdict, tell_user: bool = True) -> None:
        """Spam: block as a precaution. Overuse: warn. The admins hear about both."""
        name = html.escape(users.name(uid))
        shield.log(uid, v.kind, v.reason)
        if v.kind == guard.BLOCK:
            try:
                users.set_role(uid, BLOCKED)
            except ValueError:
                return
            log.warning("Vorsorglich gesperrt: %s (%s)", uid, v.reason)
            await notify_user(uid, f"🚫 Du bist vorsorglich gesperrt: {v.reason}. Der Admin schaut es sich an "
                                   "und kann dich wieder freigeben.")
            await tell_admins(f"🚨 <b>{name}</b> ist vorsorglich gesperrt: {html.escape(v.reason)}.\n"
                              "War das Absicht? Entsperren oder gesperrt lassen.",
                              InlineKeyboardMarkup([[InlineKeyboardButton("🔓 Entsperren", callback_data=f"u:ok:{uid}"),
                                                     InlineKeyboardButton("🗑 Entfernen", callback_data=f"u:rm:{uid}")]]),
                              html_text=True)
            return
        if tell_user:
            await notify_user(uid, f"⚠️ Hinweis: {v.reason}. Bitte etwas sparsamer – der Admin ist informiert.")
        await tell_admins(f"⚠️ <b>{name}</b> übertreibt es vielleicht: {html.escape(v.reason)}.",
                          InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Sperren", callback_data=f"u:no:{uid}")]]),
                          html_text=True)

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
    tracker.is_admin = users.is_admin

    # ---- 🩺 search done, nothing loaded: say why right away ----------------------------------
    diagnosed: dict[str, float] = {}  # key -> when last explained (several commands per request)
    lang_offers: dict[str, tuple[langpick.LangOffer, int]] = {}  # "only in Korean": offer, wanted resolution

    async def on_search_done(chat_id: int, name: str, body: dict, state: dict) -> None:
        svc = radarr if name.startswith("Movie") or "movieIds" in body else sonarr
        if svc is None:
            return
        if state.get("status") != "completed":
            debuglog.LOG.late(chat_id, "⚠️", f"{svc.kind} {name} nicht fertig: {state.get('message') or state.get('status')}")
            return
        n = diagnose.grabbed(state.get("message"))
        kind = "movie" if svc is radarr else "series"
        season = body.get("seasonNumber")
        episode_id = None
        if kind == "movie":
            item_id = (body.get("movieIds") or [None])[0]
        elif body.get("episodeIds"):
            episode_id = body["episodeIds"][0]
            ep = await sonarr._req("GET", f"/episode/{episode_id}") or {}
            item_id, season = ep.get("seriesId"), ep.get("seasonNumber")
        else:
            item_id = body.get("seriesId")
        if item_id is None:
            return
        field_ = "movieId" if kind == "movie" else "seriesId"
        if n is None:  # message without a number: look into the queue instead
            n = sum(1 for q in await svc.queue() if q.get(field_) == item_id)
        if n:
            debuglog.LOG.late(chat_id, "⬇️", f"{svc.kind}: {n} Download(s) gestartet")
            return
        key = f"{'radarr' if kind == 'movie' else 'sonarr'}:{item_id}"
        now = asyncio.get_running_loop().time()
        if now - diagnosed.get(key, -1e9) < 600:
            return
        diagnosed[key] = now
        rels: list[dict] = []
        why = await diagnose.why_nothing(svc, kind, item_id, season, episode_id, keep=rels)
        item = await svc.get(item_id)
        what = item.get("title", "?") + (f" (Staffel {season})" if season is not None else "")
        text = f"🔍 {svc.kind} hat für {what} gesucht, aber nichts geladen.\n{why}"
        debuglog.LOG.late(chat_id, "🩺", text)
        if t := store.state.tracked.get(key):
            t.no_release_notified = True  # this replaces the notice after 30 minutes
        buttons = tracker.no_release_buttons(key)
        offer = None
        if not episode_id:  # a single episode is not worth the question
            try:
                profile = next((p for p in await svc.quality_profiles() if p.get("id") == item.get("qualityProfileId")), {})
            except ArrError:
                profile = {}
            offer = langpick.build(kind, item_id, season, chat_id, item.get("title", "?"), profile.get("name", ""), rels)
        if offer is not None:
            oid = format(abs(hash((key, season, now))) % 16**8, "08x")
            lang_offers[oid] = (offer, qprofiles.max_resolution(profile) or 1080)
            while len(lang_offers) > 200:
                lang_offers.pop(next(iter(lang_offers)))
            text = offer.text()
            buttons = ([(f"⬇️ Auf {lbl} laden", f"n:lg:{oid}:{i}") for i, lbl in enumerate(offer.groups)]
                       + [("🔔 Auf Deutsch warten", f"n:w:{key}"), ("🗑 Wieder löschen", f"n:rm:{key}"),
                          ("👍 Okay", "n:ok")])
        await tracker.tell(chat_id, text, buttons, kind="nothing")

    for svc_ in (radarr, sonarr):
        if svc_ is not None:
            svc_.on_search_done = on_search_done

    # ---- 🐞 chat log -----------------------------------------------------------------------
    async def debug_send(chat_id: int, text: str) -> None:
        await app.bot.send_message(chat_id, f"🐞 {text}")

    debuglog.LOG.notify = debug_send

    async def on_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/log [name] [n]: what the bot did for the last n messages (default 1) of a chat."""
        args = list(ctx.args or [])
        n = int(args.pop()) if args and args[-1].isdigit() and len(args[-1]) < 3 else 1
        chat, title = update.effective_chat.id, ""
        if args:
            who = users.find(" ".join(args))
            if who is None:
                await update.effective_message.reply_text(f"Nutzer „{' '.join(args)}“ kenne ich nicht.")
                return
            chat, title = who, users.name(who)
        await update.effective_message.reply_text(debuglog.LOG.render(chat, tz, max(1, min(n, 10)), title),
                                                  parse_mode=ParseMode.HTML)

    async def on_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/debug an|aus: after every answer the log of it, plus late results (search done)."""
        chat = update.effective_chat.id
        arg = (ctx.args[0].lower() if ctx.args else "")
        on = arg in ("an", "on", "ein", "1") or (not arg and chat not in debuglog.LOG.live)
        (debuglog.LOG.live.add if on else debuglog.LOG.live.discard)(chat)
        await update.effective_message.reply_text(
            "🐞 Debug an: Nach jeder Antwort kommt das Log dazu, Suchergebnisse von Sonarr/Radarr sobald "
            "sie fertig sind. Aus mit /debug aus." if on else "🐞 Debug aus. Das Log gibt es weiter mit /log.")

    # ---- 🔕 mute ---------------------------------------------------------------------------
    async def on_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/stumm [Minuten | 2h | 1d]: download messages off for a while (default 60 min)."""
        raw = (ctx.args[0].lower() if ctx.args else "60")
        m = re.fullmatch(r"(\d+)\s*(m|min|h|std|d|t)?", raw)
        if not m:
            await update.effective_message.reply_text("So: /stumm 30 (Minuten), /stumm 2h oder /stumm 1d")
            return
        unit = {"h": 60, "std": 60, "d": 1440, "t": 1440}.get(m.group(2) or "", 1)
        await update.effective_message.reply_text(mute(store, update.effective_chat.id, int(m.group(1)) * unit))

    async def on_unmute(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(mute(store, update.effective_chat.id, None))

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
        now = asyncio.get_running_loop().time()
        current = (busy_with.get(chat_id) or [None])[0]
        if busy.is_status_question(text, current is not None):  # no AI, no limit: answered right away
            tool = tools.running.get(chat_id)
            await app.bot.send_message(chat_id, busy.status_text(
                current[0] if current else None, current[1] if current else None,
                tool[0] if tool else None, tool[1] if tool else None, now))
            return
        if current is not None:
            await app.bot.send_message(chat_id, f"⏳ Ich bin noch bei „{current[0][:60]}“ – deine Nachricht "
                                                "ist gleich danach dran.")
        if not users.is_admin(user_id) and (v := shield.message(user_id, text)):
            activity_db.record(user_id, text, [], activity.LIMIT, f"Spam: {v.reason}")
            await guard_verdict(user_id, v)
            return
        if reason := users.blocked_by_limit(user_id):
            if (u := users.users.get(user_id)) and (v := shield.limits(user_id, u.usage, users.limits_for(user_id))):
                await guard_verdict(user_id, v, tell_user=False)
            activity_db.record(user_id, text, [], activity.LIMIT, reason)
            await app.bot.send_message(chat_id, f"⛔ {reason}\nButtons und /downloads gehen weiterhin.")
            return
        users.count_request(user_id)
        if (u := users.users.get(user_id)) and (v := shield.limits(user_id, u.usage, users.limits_for(user_id))):
            await guard_verdict(user_id, v)
        typing = asyncio.create_task(keep_typing(chat_id))
        tools.take_trace(chat_id)
        turn_tokens.pop(user_id, None)
        answering.add(chat_id)
        busy_with.setdefault(chat_id, []).append((text, now))
        failed = None
        try:
            reply = await asyncio.wait_for(agent.handle(chat_id, text, user_id), RESPONSE_TIMEOUT)
        except asyncio.TimeoutError:
            log.error("chat %s: keine Antwort nach %ss", chat_id, RESPONSE_TIMEOUT)
            failed = f"Keine Antwort der KI nach {RESPONSE_TIMEOUT} s"
            reply = (
                f"⚠️ Ich habe nach {RESPONSE_TIMEOUT // 60} Minuten keine Antwort von der KI bekommen. "
                "Meist ist das Kontingent kurz erschöpft. Versuch es gleich nochmal."
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Fehler bei Nachricht")
            failed = f"{type(e).__name__}: {e}"
            reply = f"⚠️ Da ist intern etwas schiefgelaufen ({type(e).__name__}: {e})."
        finally:
            typing.cancel()
            answering.discard(chat_id)
            mine = busy_with.get(chat_id) or []
            if (text, now) in mine:
                mine.remove((text, now))
            if not mine:
                busy_with.pop(chat_id, None)
        await log_activity(chat_id, user_id, text, reply, failed)

        if reply.strip():
            await send_formatted(chat_id, reply)
        await deliver_outbox(chat_id, user_id, ctx)
        if chat_id in debuglog.LOG.live:
            await app.bot.send_message(chat_id, debuglog.LOG.render(chat_id, tz), parse_mode=ParseMode.HTML)

    async def log_activity(chat_id: int, user_id: int, text: str, reply: str, failed: str | None) -> None:
        """Remember what was asked and how it went; tell the admins about real errors right away."""
        trace = tools.take_trace(chat_id)
        names = [t for t, _, _ in trace]
        finals = "\n".join(f for _, _, f in trace if f)
        outcome, detail = activity.classify(names, [e for _, e, _ in trace if e], (reply or "") + finals, failed)
        activity_db.record(user_id, text, names, outcome, detail, turn_tokens.pop(user_id, 0))
        if outcome == activity.ERROR and not users.is_admin(user_id):
            await tell_admins(f"❌ Fehler bei {users.name(user_id)}: „{text[:120]}“\n{detail[:300]}\n\n"
                              "Alles dazu: /aktivitaet")

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
    buttons_file = Path(cfg.data_dir) / "buttons.json"

    def save_buttons() -> None:
        """Open choices (titles, seasons, quality) survive a restart or an update of the container."""
        try:
            tmp = buttons_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(tools.dump_buttons(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(buttons_file)
        except (OSError, TypeError, ValueError) as e:
            log.warning("Buttons nicht gespeichert: %s", e)

    def load_buttons() -> None:
        try:
            n = tools.load_buttons(json.loads(buttons_file.read_text(encoding="utf-8")))
            if n:
                log.info("%d offene Auswahlen nach dem Neustart wieder aktiv", n)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("Buttons nicht geladen: %s", e)

    async def save_buttons_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        save_buttons()

    def markup_for(buttons: list[tuple[str, str]], prefix: str) -> InlineKeyboardMarkup | None:
        if not buttons:
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=f"{prefix}:{val}")] for lbl, val in buttons])

    def offer_markup(offer: Offer) -> InlineKeyboardMarkup | None:
        p, btn = f"o:{offer.id}", InlineKeyboardButton
        shown = [(i, c) for i, c in enumerate(offer.cands) if i not in offer.hidden]
        if offer.calibrate:
            rows = [[btn(f"👍 {c.title[:30]}", callback_data=f"{p}:l{i}"), btn("👎", callback_data=f"{p}:d{i}")]
                    for i, c in shown]
            rows.append([btn("✅ Fertig – zeig mir Vorschläge", callback_data=f"{p}:go")])
        elif offer.mode == "rate":
            rows = [[btn(f"👎 {c.title[:26]}", callback_data=f"{p}:d{i}"),
                     btn("👁 Schon gesehen", callback_data=f"{p}:s{i}")] for i, c in shown]
            rows.append([btn("↩️ Fertig bewertet", callback_data=f"{p}:back")])
        else:
            rows = [[btn(c.label, callback_data=f"{p}:{i}")] for i, c in shown]
            if offer.bulk and len(offer.cands) > 1:
                rows.append([btn(f"⬇️ Alle {len(offer.cands)} hinzufügen", callback_data=f"{p}:all")])
            if offer.suggest and shown:
                rows.append([btn("🙅 Nichts davon", callback_data=f"{p}:none"),
                             btn("👎👁 Bewerten", callback_data=f"{p}:rate")])
        return InlineKeyboardMarkup(rows) if rows else None

    async def send_offer(offer: Offer) -> None:
        msg = await app.bot.send_message(offer.chat_id, offer.text, reply_markup=offer_markup(offer))
        offer.message_id = msg.message_id

    def rating_markup(ask_id: str) -> InlineKeyboardMarkup:
        stars = [InlineKeyboardButton("⭐" * n if n < 4 else f"{n}⭐", callback_data=f"rt:{ask_id}:{n}")
                 for n in range(1, 6)]
        return InlineKeyboardMarkup([stars, [InlineKeyboardButton("🤷 Weiß nicht", callback_data=f"rt:{ask_id}:0"),
                                             InlineKeyboardButton("🔕 Nicht mehr fragen", callback_data="rt:off:0")]])

    async def ask_rating(chat_id: int, ask_id: str, title: str, intro: str = "🍿 Wie fandest du") -> None:
        await app.bot.send_message(chat_id, f"{intro} <b>{html.escape(title)}</b>?", parse_mode=ParseMode.HTML,
                                   reply_markup=rating_markup(ask_id))

    async def on_rating_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not allowed_user(update):
            await query.answer("Kein Zugriff.", show_alert=True)
            return
        _, ask_id, n = query.data.split(":")
        uid = update.effective_user.id
        if ask_id == "off":
            plex_store.set_meta(f"rate_off:{uid}", "1")
            await query.answer("🔕 Frage ich nicht mehr")
            await query.edit_message_reply_markup(None)
            return
        text = tools.rate(ask_id, int(n), uid)
        await query.answer("Gemerkt" if text else "Abgelaufen")
        await query.edit_message_text(text or "⌛ Diese Frage ist abgelaufen.")

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
        if not tools.owner_ok("flow", flow_id, update.effective_user.id, query.message.chat_id):
            await query.answer("Das ist nicht deine Auswahl.", show_alert=True)
            return
        for job in ctx.job_queue.get_jobs_by_name(f"f:{flow_id}"):
            job.schedule_removal()
        await query.answer()
        flow = await tools.choose(flow_id, value)
        if flow is None:
            await query.edit_message_reply_markup(None)
            await app.bot.send_message(query.message.chat_id, "⌛ Diese Auswahl ist abgelaufen. Schreib mir einfach nochmal.")
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
        uid = update.effective_user.id
        if not tools.owner_ok("offer", offer_id, uid, query.message.chat_id):
            await query.answer("Das ist nicht deine Auswahl.", show_alert=True)
            return
        if idx in ("rate", "back") or (idx[:1] in ("l", "d", "s") and idx[1:].isdigit()):
            if idx in ("rate", "back"):
                offer, answer, ask = tools.offer_mode(offer_id, "rate" if idx == "rate" else ""), None, None
            else:
                res = tools.offer_feedback(offer_id, int(idx[1:]), idx[0], uid)
                offer, answer, ask = res if res else (None, None, None)
            if offer is None:
                await query.answer("Abgelaufen")
                await query.edit_message_reply_markup(None)
                return
            await query.answer(answer or "")
            try:
                await query.edit_message_reply_markup(offer_markup(offer))
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    raise
            if ask:
                await ask_rating(query.message.chat_id, ask, offer.cands[int(idx[1:])].title)
            return
        await query.answer()
        if idx == "go":  # quick start done: now real suggestions
            tools.offers.pop(offer_id, None)
            await query.edit_message_reply_markup(None)
            await tools.run(ToolCall("cal", "suggest", {}), uid)
            await deliver_outbox(query.message.chat_id, uid, ctx)
            return
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

    async def on_lang_pick(query: Any, oid: str, idx: int) -> None:
        """'Auf Koreanisch laden': grab the best release of that language, past the profile."""
        entry = lang_offers.get(oid)
        if entry is None or entry[0].chat_id != query.message.chat_id or idx >= len(entry[0].groups):
            await query.answer("Abgelaufen – sag einfach „such nochmal“.", show_alert=True)
            return
        offer, target = entry
        lang = list(offer.groups)[idx]
        svc = radarr if offer.kind == "movie" else sonarr
        await query.answer(f"Lade auf {lang} …")
        await query.edit_message_reply_markup(None)
        grabbed, failed = 0, []
        for r in langpick.pick(offer.groups[lang], target):
            try:
                await svc.grab(r)
                grabbed += 1
            except ArrError as e:
                failed.append(str(e))
        lang_offers.pop(oid, None)
        if not grabbed:
            text = f"❌ Auf {lang} ging es nicht: {failed[0] if failed else 'kein passendes Release'}"
        else:
            what = offer.title + (f" (Staffel {offer.season})" if offer.season is not None else "")
            text = (f"⬇️ {what}: {grabbed} Release{'s' if grabbed > 1 else ''} auf {lang} geholt. "
                    "Ich melde mich, wenn der Download startet.")
            if offer.kind == "series":
                text += (f"\nℹ️ Weitere Staffeln und neue Folgen sucht Sonarr weiter mit "
                         f"„{offer.profile or 'deinem Profil'}“ – also auf Deutsch. Willst du alles auf {lang}, "
                         "stell die Serie in Sonarr auf ein Profil ohne Deutsch-Pflicht.")
            if failed:
                text += f"\n⚠️ {len(failed)} nicht geholt: {failed[0]}"
        await app.bot.send_message(query.message.chat_id, text)

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
        uid = update.effective_user.id
        if parts[1] == "lg":
            await on_lang_pick(query, parts[2], int(parts[3]))
            return
        action, service, raw_id = parts[1], parts[2], int(parts[3])
        kind = "movie" if service == "radarr" else "series"
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

    async def plex_check() -> str:
        acc = next((a for a in plex_store.all() if users.is_admin(a.user_id)), None) or next(iter(plex_store.all()))
        info = await plex_client._srv(acc, "/identity")
        return f"Server {acc.server_name} antwortet (Version {(info.get('MediaContainer') or info).get('version', '?')})"

    def plex_probe():
        return plex_check if plex_store.all() else None

    monitor = health.Monitor()
    arr_watch = arrhealth.HealthWatch(cfg.data_dir)
    arr_services = {s.kind: s for s in (sonarr, radarr) if s is not None}
    update_state: dict = {"latest": None, "told": None}

    async def check_text() -> str:
        checks = await health.run_checks(bot=app.bot, provider=agent.provider, model=cfg.llm.model,
                                         radarr=radarr, sonarr=sonarr, sab=sab, tmdb=tmdb, plex=plex_probe(),
                                         builder=tools.builder if cfg.recipe_model != cfg.llm.model else None,
                                         builder_model=cfg.recipe_model)
        update_state["latest"] = await version.latest(cfg.update_repo, token=cfg.update_token) or update_state["latest"]
        extra = [version.line(update_state["latest"]), *model_lines(True)]
        if cfg.monitor_minutes > 0:
            extra.append(monitor.status() + f" (alle {cfg.monitor_minutes} Min.)")
        for svc in arr_services.values():
            try:
                found = await arrhealth.issues(svc)
            except ArrError:
                continue
            extra.append(f"🩺 {svc.kind} meldet: " + ("nichts ✅" if not found else ""))
            extra += [f"   {arrhealth.WEIGHT.get(i.kind, '⚠️')} {i.message}" for i in found]
        return health.render(checks) + "\n\n" + "\n".join(html.escape(x) for x in extra)

    async def monitor_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        checks = await health.run_checks(bot=None, provider=None, model="", radarr=radarr, sonarr=sonarr,
                                         sab=sab, tmdb=tmdb, plex=plex_probe(), light=True)
        for msg in monitor.update(checks):
            log.warning("Überwachung: %s", msg)
            await tell_admins(msg)
        await arr_health_round()

    async def arr_health_round() -> None:
        """Sonarr/Radarr warnings: new ones (fixed if harmless), and what is gone."""
        new, gone = await arr_watch.poll(list(arr_services.values()))
        for issue in new:
            text = issue.text()
            svc = arr_services.get(issue.service)
            if svc is not None and issue.source in arrhealth.AUTO_FIX:
                if fix := await arrhealth.auto_fix(svc, issue):
                    text += "\n" + fix
            rows = []
            if issue.source == "IndexerCategories" and issue.indexer_id is not None:
                rows.append([InlineKeyboardButton("🔧 Unterkategorien eintragen",
                                                  callback_data=f"h:cat:{issue.service}:{issue.indexer_id}")])
            log.warning("%s meldet: %s", issue.service, issue.message)
            await tell_admins(text, InlineKeyboardMarkup(rows) if rows else None)
        for g in gone:
            await tell_admins(f"✅ {g.get('service')}: behoben – {g.get('message')}")

    async def on_health_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not (update.effective_user and users.is_admin(update.effective_user.id)):
            await query.answer("Nur für den Admin.", show_alert=True)
            return
        _, action, service, raw = query.data.split(":", 3)
        await query.answer()
        svc = arr_services.get(service)
        if action != "cat" or svc is None:
            return
        try:
            text = await arrhealth.add_subcategories(svc, int(raw))
        except ArrError as e:
            text = f"❌ Ging nicht: {e}\nBitte in {service} unter Settings → Indexers von Hand anhaken."
        await query.edit_message_reply_markup(None)
        await app.bot.send_message(query.message.chat_id, text)

    async def update_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        latest = await version.latest(cfg.update_repo, token=cfg.update_token)
        if not latest:
            return
        update_state["latest"] = latest
        if version.newer(latest, version.__version__) and update_state["told"] != latest:
            update_state["told"] = latest
            await tell_admins(f"⬆️ Neue Bot-Version {latest} verfügbar (läuft: {version.__version__}).\n"
                              "Container neu bauen bzw. Image ziehen und neu starten. Details: /version")

    def models() -> tuple[admin_view.Model, admin_view.Model | None]:
        main = admin_view.model_of(agent.provider, cfg.llm.model)
        builder = admin_view.model_of(tools.builder, cfg.recipe_model) if tools.builder else None
        return main, builder

    def model_lines(is_admin_: bool) -> list[str]:
        main, builder = models()
        out = [f"🧠 KI ({cfg.llm.provider}): {main.text(PRICES if is_admin_ else {})}"]
        if is_admin_ and builder and builder.name != main.name:
            out.append(f"🛠 Werkzeug-KI: {builder.text(PRICES)}")
        return out

    async def bot_info(uid: int) -> str:
        """Version and models; admins also get the update check and what is new."""
        is_adm = users.is_admin(uid)
        if is_adm:
            update_state["latest"] = await version.latest(cfg.update_repo, token=cfg.update_token) or update_state["latest"]
        main, builder = models()
        changes = ""
        if is_adm:
            vt = version.text(update_state["latest"]).split("\n", 1)
            changes = vt[1].strip() if len(vt) > 1 else ""
        return admin_view.info_text(version.line(update_state["latest"] if is_adm else None), main, builder, PRICES,
                               cfg.llm.provider, is_adm, changes)

    tools.bot_info = bot_info

    async def on_version(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(await bot_info(update.effective_user.id))

    # ---- admin overview and user card -------------------------------------------------------------
    ADMIN_VIEWS = [("👥 Nutzer", "users"), ("⏳ Freigaben", "approvals"), ("📊 Statistik", "stats"),
                   ("📋 Aktivität", "activity"), ("🩺 Check", "check"), ("🗓 Monatsbericht", "monthly"),
                   ("🧹 Regeln", "cleanup"), ("🎞 Mediathek", "media"), ("🛠 Werkzeuge", "recipes")]

    def admin_text() -> tuple[str, InlineKeyboardMarkup]:
        week = utcnow() - timedelta(days=7)
        text = admin_view.dashboard(
            version_line=version.line(update_state["latest"]), models=model_lines(True), users=users,
            pending_approvals=len(approvals.pending()), entries=activity_db.entries(days=7),
            events=shield.events(week), monitor_status=monitor.status(), recipes=len(tools.recipes.all()))
        rows = [[InlineKeyboardButton(label, callback_data=f"g:{key}") for label, key in ADMIN_VIEWS[i:i + 3]]
                for i in range(0, len(ADMIN_VIEWS), 3)]
        for u in users.listing():
            if u.role == PENDING:
                rows.append([InlineKeyboardButton(f"✅ {u.name[:18]} freigeben", callback_data=f"u:ok:{u.id}"),
                             InlineKeyboardButton("🗑", callback_data=f"u:rm:{u.id}")])
        rows.append([InlineKeyboardButton("🔄 Aktualisieren", callback_data="g:refresh")])
        return text, InlineKeyboardMarkup(rows)

    async def send_admin(chat_id: int) -> None:
        text, markup = admin_text()
        await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def on_admin(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_admin(update.effective_chat.id)

    def user_card_text(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
        u = users.users.get(uid)
        if u is None:
            return "Diesen Nutzer gibt es nicht mehr.", None
        month = utcnow() - timedelta(days=30)
        extra = []
        acc = plex_store.get(uid)
        extra.append("\n🎬 Plex: " + (f"verbunden ({html.escape(acc.server_name)})" if acc else "nicht verbunden"))
        n_wish, n_sites = len(wishes.wishes(uid)), len(sites.sites(uid))
        if n_wish or n_sites:
            extra.append(f"⭐ Wunschliste: {n_wish} · 🌐 beobachtete Seiten: {n_sites}")
        rules_ = [r for r in approvals.rules() if r.user_id in (None, uid)]
        if rules_ and u.role != ADMIN:
            extra.append(f"⏳ {len(rules_)} Freigabe-Regel{'n' if len(rules_) != 1 else ''} gelten für {html.escape(u.name)}")
        text = admin_view.user_card(u, users, store.state.requests, activity_db.entries(days=30, user_id=uid),
                               [e for e in shield.events(month) if e.user_id == uid], extra, tz)
        if u.role == ADMIN:
            return text, None
        first = (InlineKeyboardButton("🔓 Freigeben", callback_data=f"u:ok:{uid}") if u.role in (PENDING, BLOCKED)
                 else InlineKeyboardButton("🚫 Sperren", callback_data=f"u:no:{uid}"))
        return text, InlineKeyboardMarkup([[first, InlineKeyboardButton("🗑 Entfernen", callback_data=f"u:rm:{uid}")]])

    async def send_user_card(chat_id: int, uid: int) -> None:
        text, markup = user_card_text(uid)
        await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def on_admin_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        viewer = update.effective_user.id if update.effective_user else 0
        if not users.is_admin(viewer):
            await query.answer("Nur für Admins.", show_alert=True)
            return
        key = query.data.split(":", 1)[1]
        await query.answer()
        chat_id = query.message.chat_id
        if key == "refresh":
            text, markup = admin_text()
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except BadRequest:
                pass  # unchanged
        elif key == "monthly":
            await app.bot.send_message(chat_id, monthly_text(monthly.previous_month(datetime.now(tz).date())),
                                       parse_mode=ParseMode.HTML)
        elif key == "recipes":
            await app.bot.send_message(chat_id, recipes.render_list(tools.recipes.all()), parse_mode=ParseMode.HTML)
        else:
            await send_view(View(chat_id, key), viewer)

    async def send_check(chat_id: int) -> None:
        msg = await app.bot.send_message(chat_id, "🩺 Prüfe alle APIs …")
        await app.bot.edit_message_text(await check_text(), chat_id, msg.message_id, parse_mode=ParseMode.HTML)

    async def on_check(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_check(update.effective_chat.id)

    def notify_markup(chat_id: int) -> InlineKeyboardMarkup:
        b = notify.buttons(store, chat_id)
        rows = [[InlineKeyboardButton(lbl, callback_data=d) for lbl, d in b[i:i + 2]] for i in range(0, len(b), 2)]
        return InlineKeyboardMarkup(rows)

    async def send_notify(chat_id: int) -> None:
        await app.bot.send_message(chat_id, notify.render(store, chat_id), reply_markup=notify_markup(chat_id))

    async def on_notify_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_notify(update.effective_chat.id)

    async def on_notify_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not allowed_user(update):
            await query.answer("Kein Zugriff.", show_alert=True)
            return
        kind = query.data.split(":", 1)[1]
        chat = query.message.chat_id
        cur = notify.prefs(store, chat)
        notify.change(store, chat, **{kind: not cur.get(kind, True)})
        await query.answer("Gespeichert")
        await query.edit_message_text(notify.render(store, chat), reply_markup=notify_markup(chat))

    async def send_view(view: View, viewer: int) -> None:
        if view.kind == "html":
            await app.bot.send_message(view.chat_id, view.arg, parse_mode=ParseMode.HTML)
        elif view.kind == "notify":
            await send_notify(view.chat_id)
        elif view.kind == "downloads":
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
        elif view.kind == "approvals" and users.is_admin(viewer):
            await send_approvals(view.chat_id)
        elif view.kind == "admin" and users.is_admin(viewer):
            await send_admin(view.chat_id)
        elif view.kind == "usercard" and users.is_admin(viewer):
            await send_user_card(view.chat_id, int(view.arg))
        elif view.kind == "cleanup" and users.is_admin(viewer):
            if view.arg:
                n = await run_cleanup(view.chat_id)
                if not n:
                    await app.bot.send_message(view.chat_id, "🧹 Keine Regel trifft gerade etwas. 👍")
            else:
                await send_cleanup(view.chat_id)
        elif view.kind == "activity" and users.is_admin(viewer):
            mode, days, who = view.arg or ("all", 7, None)
            await send_activity(view.chat_id, mode or "all", days or 7, who)
        elif view.kind == "media" and users.is_admin(viewer):
            section, days = view.arg or ("overview", None)
            if days:
                media_state["days"] = int(days)
            await send_media(view.chat_id, section or "overview")

    # ---- approvals (admin) ------------------------------------------------------------------------
    def approval_buttons(pid: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Freigeben", callback_data=f"r:ok:{pid}"),
                                      InlineKeyboardButton("🙅 Ablehnen", callback_data=f"r:no:{pid}")]])

    async def ask_approval(p: approval.Pending) -> None:
        await tell_admins(f"✋ {users.name(p.user_id)} wünscht sich: {p.title}\nGrund für die Nachfrage: {p.reason}",
                          approval_buttons(p.id))

    tools.on_approval = ask_approval

    async def send_approvals(chat_id: int) -> None:
        rs, pending = approvals.rules(), approvals.pending()
        rows = [[InlineKeyboardButton(f"❌ Regel {r.id} löschen", callback_data=f"r:del:{r.id}")] for r in rs]
        await app.bot.send_message(chat_id, approval.render_rules(rs, pending, users.name),
                                   reply_markup=InlineKeyboardMarkup(rows) if rows else None)
        for p in pending[:10]:  # each open wish with its own buttons
            await app.bot.send_message(chat_id, f"⏳ {users.name(p.user_id)}: {p.title} – {p.reason}",
                                       reply_markup=approval_buttons(p.id))

    async def on_approval_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not (update.effective_user and users.is_admin(update.effective_user.id)):
            await query.answer("Nur für Admins.", show_alert=True)
            return
        _, action, arg = query.data.split(":", 2)
        if action == "del":
            approvals.remove(int(arg))
            await query.answer("Regel gelöscht")
            await query.edit_message_text(approval.render_rules(approvals.rules(), approvals.pending(), users.name))
            return
        await query.answer("Moment …")
        p, text = await tools.decide(arg, action == "ok")
        if p and action == "ok" and not text.startswith("❌"):
            activity_db.record(p.user_id, f"Freigabe: {p.title}", [], activity.ACTION)
        try:
            await query.edit_message_text(f"{query.message.text}\n\n{text}" if query.message else text)
        except BadRequest:
            pass

    async def on_approvals(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_approvals(update.effective_chat.id)

    async def on_recipes(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(recipes.render_list(tools.recipes.all()), parse_mode=ParseMode.HTML)

    # ---- cleanup rules (admin) --------------------------------------------------------------------
    def cleanup_markup() -> InlineKeyboardMarkup | None:
        rows = [[InlineKeyboardButton(f"❌ Regel {r.id} löschen", callback_data=f"c:del:{r.id}")] for r in rules.rules()]
        if rows:
            rows.append([InlineKeyboardButton("▶️ Jetzt prüfen", callback_data="c:run")])
        return InlineKeyboardMarkup(rows) if rows else None

    async def send_cleanup(chat_id: int) -> None:
        await app.bot.send_message(chat_id, cleanup.render_rules(rules.rules()), parse_mode=ParseMode.HTML,
                                   reply_markup=cleanup_markup())

    async def cleanup_hits(rule: cleanup.Rule, fresh: bool = False) -> list[media.Title]:
        if rule.low_space_gb:
            ds = await extras.disks(radarr, sonarr)
            if not extras.low_disks(ds, rule.low_space_gb):
                return []
        return cleanup.matches(rule, await media_report(fresh=fresh), rules.kept())

    async def delete_titles(hits: list[media.Title]) -> list[str]:
        failed = []
        for t in hits:
            svc = radarr if t.kind == "movie" else sonarr
            try:
                await svc.delete(t.arr_id, delete_files=True)
            except ArrError as e:
                failed.append(f"{t.name}: {e}")
        media_state["report"] = None
        return failed

    async def run_cleanup(chat_id: int | None = None) -> int:
        """Apply every rule. chat_id: only to this admin (manual run), else to all admins. Returns hits."""
        total = 0
        for rule in rules.rules():
            try:
                hits = await cleanup_hits(rule, fresh=True)
            except Exception as e:  # noqa: BLE001
                log.exception("Aufräumregel %s", rule.id)
                await tell_admins(f"⚠️ Aufräumregel {rule.id} ging nicht: {e}")
                continue
            if not hits:
                continue
            total += len(hits)
            if rule.auto and chat_id is None:
                failed = await delete_titles(hits)
                text, _ = cleanup.render_hits(rule, hits, deleted=True)
                if failed:
                    text += "\n⚠️ Nicht gelöscht: " + "; ".join(failed[:5])
                await tell_admins(text)
                continue
            text, buttons = cleanup.render_hits(rule, hits)
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=d)] for lbl, d in buttons])
            for admin in ([chat_id] if chat_id else users.admins()):
                try:
                    await app.bot.send_message(admin, text, reply_markup=markup)
                except (BadRequest, Forbidden) as e:
                    log.info("Aufräumen an %s: %s", admin, e)
        return total

    def monthly_text(month: str) -> str:
        start, _ = monthly.bounds(month, tz)
        entries = activity_db.entries(days=(utcnow() - start).days + 2)
        return monthly.render(month, users, store.state.requests, entries,
                              shield.events(start - timedelta(days=1)), tools.recipes.all(), tz)

    async def monthly_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        """1st of the month: the report for last month to every admin."""
        today = datetime.now(tz).date()
        if today.day == 1:
            await tell_admins(monthly_text(monthly.previous_month(today)), html_text=True)

    async def on_monthly(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        today = datetime.now(tz).date()
        now = bool(ctx.args) and ctx.args[0].lower() in ("jetzt", "aktuell", "diesen")
        month = today.strftime("%Y-%m") if now else monthly.previous_month(today)
        await update.effective_message.reply_text(monthly_text(month), parse_mode=ParseMode.HTML)

    async def watch_ask_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Now and then: 'Du hast Dune gesehen – löschen?' (admins with a linked Plex)."""
        from . import watchask

        s = watchask.load(rules)
        if not watchask.due(rules, s):
            return
        sent = 0
        for admin in users.admins():
            try:
                n = await tools.watch_questions(admin)
            except (ArrError, plex.PlexError) as e:
                log.info("Nachfragen zu Gesehenem: %s", e)
                continue
            if n:
                await deliver_outbox(admin, admin, ctx)
                sent += n
        if sent:
            watchask.mark_round(rules)

    async def unusual_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        """Evening: anyone with far more requests today than the others (admins only hear about it)."""
        today = datetime.now().strftime("%Y-%m-%d")
        counts = {u.id: (u.usage.day_requests if u.usage.day == today else 0) for u in users.users.values()
                  if u.role == USER}
        for uid, v in shield.unusual(counts):
            await guard_verdict(uid, v, tell_user=False)

    async def wish_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        """Daily: wishes that got a release date are added (no AI)."""
        for uid, text in await wishlist.check(tools, wishes):
            if not users.is_allowed(uid):
                continue
            try:
                await app.bot.send_message(uid, text)
            except (BadRequest, Forbidden) as e:
                log.info("Wunschliste an %s nicht zugestellt: %s", uid, e)

    async def sites_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Look at every watched page; only changed pages cost tokens."""
        for site in sites.sites():
            if not users.is_allowed(site.user_id) or users.blocked_by_limit(site.user_id):
                continue
            try:
                usage = await tools.check_site(site, agent.provider)
            except Exception as e:  # noqa: BLE001 - one broken page must not stop the others
                log.info("Seite %s: %s", site.url, e)
                continue
            if usage:
                tokens = (usage.get("input", 0) or 0) + (usage.get("output", 0) or 0)
                users.add_usage(site.user_id, tokens, agent.cost_of(usage))
                agent.usage.add(usage)
            try:
                await deliver_outbox(site.user_id, site.user_id, ctx)
            except (BadRequest, Forbidden) as e:
                log.info("Seiten-Treffer an %s nicht zugestellt: %s", site.user_id, e)

    async def cleanup_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        if rules.rules():
            await run_cleanup()

    async def on_cleanup_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not (update.effective_user and users.is_admin(update.effective_user.id)):
            await query.answer("Nur für Admins.", show_alert=True)
            return
        parts = query.data.split(":")
        action = parts[1]
        if action == "del":
            rules.remove(int(parts[2]))
            await query.answer("Regel gelöscht")
            await query.edit_message_text(cleanup.render_rules(rules.rules()), parse_mode=ParseMode.HTML,
                                          reply_markup=cleanup_markup())
        elif action == "run":
            await query.answer("Prüfe …")
            if not await run_cleanup(query.message.chat_id):
                await app.bot.send_message(query.message.chat_id, "🧹 Keine Regel trifft gerade etwas. 👍")
        elif action == "keep":
            rules.keep(parts[2], int(parts[3]))
            await query.answer("🛡 Wird nie automatisch gelöscht.")
        elif action == "all":
            rule = next((r for r in rules.rules() if r.id == int(parts[2])), None)
            if not rule:
                await query.answer("Die Regel gibt es nicht mehr.", show_alert=True)
                return
            await query.answer("Lösche …")
            hits = await cleanup_hits(rule, fresh=True)  # again: something may have changed meanwhile
            failed = await delete_titles(hits)
            text, _ = cleanup.render_hits(rule, hits, deleted=True)
            if failed:
                text += "\n⚠️ Nicht gelöscht: " + "; ".join(failed[:5])
            await query.edit_message_text(text)
        else:
            await query.answer()

    async def on_cleanup(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send_cleanup(update.effective_chat.id)

    # ---- activity (admin) -------------------------------------------------------------------------
    def activity_view(mode: str = "all", days: int = 7, who: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
        entries = activity_db.entries(days, who)
        text = activity.render(entries, days, tz, users.name, mode, users.name(who) if who else None)
        w = who or ""

        def mark(m: str, label: str) -> str:
            return ("• " + label) if m == mode else label

        rows = [[InlineKeyboardButton(mark("all", "📋 Alles"), callback_data=f"a:all:{days}:{w}"),
                 InlineKeyboardButton(mark("problems", "⚠️ Probleme"), callback_data=f"a:problems:{days}:{w}"),
                 InlineKeyboardButton(mark("users", "👥 Nutzer"), callback_data=f"a:users:{days}:{w}")],
                [InlineKeyboardButton(("• " if d == days else "") + label, callback_data=f"a:{mode}:{d}:{w}")
                 for d, label in ((1, "Heute"), (7, "7 Tage"), (30, "30 Tage"))]]
        if who:
            rows.append([InlineKeyboardButton("👥 Alle Nutzer", callback_data=f"a:{mode}:{days}:")])
        return text, InlineKeyboardMarkup(rows)

    async def send_activity(chat_id: int, mode: str = "all", days: int = 7, who: int | None = None) -> None:
        text, markup = activity_view(mode, days, who)
        await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def on_activity(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        who = users.find(" ".join(ctx.args)) if ctx.args else None
        if ctx.args and who is None:
            await update.effective_message.reply_text(f"Nutzer „{' '.join(ctx.args)}“ kenne ich nicht.")
            return
        await send_activity(update.effective_chat.id, who=who)

    async def on_activity_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not (update.effective_user and users.is_admin(update.effective_user.id)):
            await query.answer("Nur für Admins.", show_alert=True)
            return
        _, mode, days, who = (query.data.split(":") + ["", "", ""])[:4]
        await query.answer()
        text, markup = activity_view(mode or "all", int(days or 7), int(who) if who else None)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

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
            text, buttons = media.render_section(r, section, days, users.name, listener.ready)
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

    async def hear_title(chat_id: int, kind: str, arr_id: int) -> None:
        """🎧 Listen to the files of one title that don't look German (background task)."""
        try:
            if kind == "m":
                movie = await radarr.get(arr_id)
                title = f"{movie.get('title')} ({movie.get('year')})"
                mfile = movie.get("movieFile") or {}
                text, bad = await listen.hear_files(listener, "m", arr_id, [mfile] if mfile else [], title, False)
                ext = movie.get("tmdbId")
            else:
                series = await sonarr.get(arr_id)
                title, ext = series.get("title", "?"), series.get("tvdbId")
                files = await sonarr.episode_files(arr_id)
                eps = {e.get("episodeFileId"): f"S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}"
                       for e in await sonarr.episodes(arr_id) if e.get("episodeFileId")}
                checks = {f["id"]: german.file_check(f) for f in files}
                todo = [{**f, "_eps": eps.get(f["id"], "")} for f in files if checks[f["id"]] != "german"]
                todo.sort(key=lambda f: f["_eps"])
                suspect = bool(todo) and all(checks[f["id"]] == "mislabelled" for f in todo) \
                    and "german" not in checks.values()
                text, bad = await listen.hear_files(listener, "s", arr_id, todo, title, suspect)
        except (ArrError, KeyError) as e:
            await app.bot.send_message(chat_id, f"⚠️ Reinhören ging nicht: {e}")
            return
        media_state["report"] = None  # results changed
        markup = None
        if bad and ext:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                "🇩🇪 Deutsch suchen und ersetzen", callback_data=f"m:gf:{kind}:{ext}")]])
        await app.bot.send_message(chat_id, text, reply_markup=markup)

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
        if action == "hear" and len(parts) == 4:
            if not listener.ready:
                await query.answer(listener.why_not()[:190], show_alert=True)
                return
            await query.answer("🎧 Ich höre rein – das dauert etwas, ich melde mich.")
            task = asyncio.create_task(hear_title(query.message.chat_id, parts[2], int(parts[3])))
            background.add(task)
            task.add_done_callback(background.discard)
            return
        if action == "ok" and len(parts) == 4:  # admin listened: it is German, only the tags are wrong
            german.confirmed.add(parts[2], int(parts[3]))
            r = media_state["report"]
            for t in (r.titles if r else []):
                if t.kind[0] == parts[2] and t.arr_id == int(parts[3]):
                    t.not_german = t.mislabelled = 0
            await query.answer("👍 Gemerkt – wird nicht mehr gemeldet.")
            if r:
                text, markup = media_view(r, "not_german")
                try:
                    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
                except BadRequest:
                    pass
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

    def plex_markup(uid: int) -> InlineKeyboardMarkup:
        off = plex_store.get_meta(f"new_off:{uid}") is not None
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Jetzt abgleichen", callback_data="x:s"),
                                      InlineKeyboardButton("🔌 Trennen", callback_data="x:u")],
                                     [InlineKeyboardButton("🔔 „Neu in Plex“ einschalten" if off else
                                                           "🔕 „Neu in Plex“ ausschalten",
                                                           callback_data="x:non" if off else "x:noff")]])

    def plex_status_text(a: plex.Account) -> str:
        when = f", zuletzt abgeglichen {a.synced[:16].replace('T', ' ')} UTC" if a.synced else ""
        return (f"✅ Plex verbunden als <b>{html.escape(a.username)}</b> mit „{html.escape(a.server_name)}“{when}.\n"
                "Ich lerne, was du schaust, und beantworte „Wo war ich bei …?“.")

    async def plex_sync(a: plex.Account, ask: bool = False) -> int:
        """ask: after the regular sync, "wie fandest du X?" for up to 2 titles just watched."""
        fresh: list[plex.Item] = []
        try:
            n = await plex.sync(plex_client, plex_store, profile_db, a, fresh)
            log.info("Plex-Abgleich %s: %d neu gesehen", a.username, n)
        except plex.PlexError as e:
            log.warning("Plex-Abgleich %s: %s", a.username, e)
            raise
        if ask and fresh and users.is_allowed(a.user_id) and not plex_store.get_meta(f"rate_off:{a.user_id}"):
            asked = profile_db.asked(a.user_id)
            for it in [x for x in fresh if (x.kind, x.tmdb) not in asked][:2]:
                c = Candidate(it.kind, it.tmdb if it.kind == "movie" else (it.tvdb or 0), it.title, it.year)
                c.tmdb_id, c.genres = it.tmdb, list(it.genres or [])
                try:
                    await ask_rating(a.user_id, tools.ask_rating(a.user_id, c), it.title,
                                     "🍿 Du hast gerade gesehen:" if it.kind == "movie" else "🍿 Durchgeschaut:")
                except (BadRequest, Forbidden) as e:
                    log.info("Bewertungsfrage an %s nicht zugestellt: %s", a.user_id, e)
                    break
        return n

    async def start_plex_link(chat_id: int, uid: int) -> None:
        acc = plex_store.get(uid)
        if acc:
            await app.bot.send_message(chat_id, plex_status_text(acc), parse_mode=ParseMode.HTML,
                                       reply_markup=plex_markup(uid))
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
        if action in ("noff", "non"):
            plex_store.set_meta(f"new_off:{uid}", "1" if action == "noff" else None)
            await query.answer("🔕 Keine „Neu in Plex“-Nachrichten mehr" if action == "noff" else
                               "🔔 Ich sage dir jeden Abend, was neu in Plex ist")
            if query.message.text and query.message.text.startswith("✅ Plex verbunden"):
                await query.edit_message_reply_markup(plex_markup(uid))
            else:
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
                await plex_sync(a, ask=True)
            except plex.PlexError:
                pass
        if plex_store.get_meta("auto_collections"):
            try:
                await tools.sync_wish_collections()
            except plex.PlexError as e:
                log.info("Wunsch-Sammlungen: %s", e)

    async def new_in_plex_job(_: ContextTypes.DEFAULT_TYPE) -> None:
        """Every evening: what came into Plex since yesterday, per user (their taste first)."""
        now = int(datetime.now(timezone.utc).timestamp())
        for a in plex_store.all():
            if not users.is_allowed(a.user_id) or plex_store.get_meta(f"new_off:{a.user_id}"):
                continue
            since = int(plex_store.get_meta(f"new_since:{a.user_id}") or now - 24 * 3600)
            try:
                items = await plex_client.recently_added(a, since)
            except plex.PlexError as e:
                log.info("Neu in Plex für %s: %s", a.username, e)
                continue
            plex_store.set_meta(f"new_since:{a.user_id}", str(now))
            prefs = profile_db.profile(a.user_id).prefs
            text, shown = plex.new_text(items, prefs.like, prefs.avoid, norm=profiles.genre_de)
            if not text:
                continue
            rows = [[InlineKeyboardButton(f"▶️ {n.title[:30]}", url=plex_client.web_link(a, n.rating_key))]
                    for n in shown[:4]]
            rows.append([InlineKeyboardButton("🔕 Nicht mehr melden", callback_data="x:noff")])
            try:
                await app.bot.send_message(a.user_id, text, parse_mode=ParseMode.HTML,
                                           reply_markup=InlineKeyboardMarkup(rows))
            except (BadRequest, Forbidden) as e:
                log.info("Neu in Plex an %s nicht zugestellt: %s", a.user_id, e)

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
            # Told once already: no answer to every further message (and never anything for the AI).
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
                "👋 Hallo! Ich habe den Admin gefragt, ob du mich nutzen darfst. Du bekommst Bescheid, sobald "
                "er entschieden hat – bis dahin antworte ich nicht."
            )

    def users_text() -> str:
        lines = ["<b>👥 Nutzer</b>"]
        for u in users.listing():
            u.usage.roll()
            lim = users.limits_for(u.id)
            lim_txt = ", ".join(limit_text(k, v) for k, v in lim.items()) or "keine Limits"
            cost = f"{u.usage.month_cost:.3f} $".replace(".", ",")
            cap = users.max_quality(u.id)
            if cap:
                lim_txt += f", höchstens {quality_label(cap)}"
            lines.append(
                f"\n{ROLE_LABEL.get(u.role, u.role)} <b>{html.escape(u.name)}</b> <code>{u.id}</code>\n"
                f"   heute {u.usage.day_requests} · Monat {u.usage.month_requests} Anfragen · {cost}\n"
                f"   <i>{html.escape(lim_txt)}</i>"
            )
        std = ", ".join(limit_text(k, v) for k, v in users.defaults.items()) or "keine"
        tot = ", ".join(limit_text(k, v) for k, v in users.total_limits.items()) or "keins"
        users.total.roll()
        lines.append(f"\n<b>Standard-Limits</b> für Nutzer: {html.escape(std)}"
                     + (f", höchstens {quality_label(users.default_quality)}" if users.default_quality else ""))
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
            info = InlineKeyboardButton("ℹ️", callback_data=f"u:info:{u.id}")
            if u.role in (PENDING, BLOCKED):
                rows.append([InlineKeyboardButton(f"✅ {short} freigeben", callback_data=f"u:ok:{u.id}"), info,
                             InlineKeyboardButton("🗑", callback_data=f"u:rm:{u.id}")])
            else:
                rows.append([InlineKeyboardButton(f"🚫 {short} sperren", callback_data=f"u:no:{u.id}"), info,
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
        if action == "info":
            await query.answer()
            await send_user_card(query.message.chat_id, uid)
            return
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
            "ART: KI-Nachrichten <code>tag</code>, <code>monat</code>, <code>tokens</code>, <code>kosten</code> "
            "(pro Monat), <code>tokens_tag</code>, <code>kosten_tag</code> (pro Tag) – "
            "oder Titel: <code>filme_</code>, <code>serien_</code>, <code>staffeln_</code>, <code>folgen_</code> + "
            "<code>stunde</code>/<code>tag</code>/<code>woche</code>/<code>monat</code> "
            "(z. B. <code>/limit Sarah filme_tag 3</code>)\n"
            "WERT: Zahl oder <code>aus</code>"
        )
        if args and args[0].lower().startswith(("empf", "tipp", "vorschlag")):
            await tools.run(ToolCall("adv", "admin_limit_advice", {}), update.effective_user.id)
            await deliver_outbox(update.effective_chat.id, update.effective_user.id, ctx)
            return
        if len(args) != 3:
            await app.bot.send_message(update.effective_chat.id, usage + "\n\n💡 <code>/limit empfehlung</code> – "
                                       "ich schlage Limits aus dem bisherigen Verbrauch vor.", parse_mode=ParseMode.HTML)
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
        return text + "\n\n" + html.escape(version.line(update_state["latest"] if users.is_admin(uid) else None)), \
            ParseMode.HTML

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
            BotCommand("stumm", "Download-Meldungen stumm (z. B. /stumm 30)"),
            BotCommand("laut", "Meldungen wieder an"),
            BotCommand("meldungen", "Welche Download-Meldungen du bekommst"),
            BotCommand("version", "Version und KI-Modell"),
            BotCommand("hilfe", "Was kann ich?"),
        ]
        await application.bot.set_my_commands(base, scope=BotCommandScopeDefault())
        for admin in users.admins():
            try:
                await application.bot.set_my_commands(
                    [BotCommand("admin", "Übersicht mit Buttons zu allem")]
                    + [c for c in base if c.command != "version"]
                    + [BotCommand("nutzer", "Nutzer verwalten"), BotCommand("statistik", "Statistik"),
                       BotCommand("check", "APIs und KI prüfen"), BotCommand("limit", "Limits setzen"),
                       BotCommand("mediathek", "Mediathek prüfen und aufräumen"),
                       BotCommand("aktivitaet", "Was Nutzer fragen, was klappte, wo es hakte"),
                       BotCommand("regeln", "Aufräumregeln"),
                       BotCommand("freigaben", "Wünsche erst freigeben"),
                       BotCommand("rezepte", "Selbstgebaute Werkzeuge"),
                       BotCommand("monatsbericht", "Bericht für den letzten Monat"),
                       BotCommand("log", "Was der Bot bei der letzten Nachricht gemacht hat"),
                       BotCommand("debug", "Log nach jeder Antwort an/aus"),
                       BotCommand("version", "Version, Modelle und Updates")],
                    scope=BotCommandScopeChat(admin),
                )
            except BadRequest as e:  # admin never wrote to the bot yet
                log.info("Admin-Befehle für %s nicht gesetzt: %s", admin, e)
        application.job_queue.run_repeating(poll_job, interval=cfg.poll_interval, first=5)
        application.job_queue.run_daily(watch_job, time(9, 0, tzinfo=tz), name="deutsch")
        application.job_queue.run_daily(digest_job, time(10, 0, tzinfo=tz), name="wochenrueckblick")
        application.job_queue.run_repeating(plex_sync_job, interval=6 * 3600, first=600, name="plex")
        application.job_queue.run_daily(new_in_plex_job, time(18, 0, tzinfo=tz), name="neu_in_plex")
        application.job_queue.run_daily(cleanup_job, time(11, 0, tzinfo=tz), name="aufraeumen")
        application.job_queue.run_daily(wish_job, time(9, 30, tzinfo=tz), name="wunschliste")
        application.job_queue.run_daily(monthly_job, time(9, 15, tzinfo=tz), name="monatsbericht")
        application.job_queue.run_daily(unusual_job, time(20, 0, tzinfo=tz), name="auffaellig")
        application.job_queue.run_daily(watch_ask_job, time(19, 0, tzinfo=tz), name="gesehen")
        application.job_queue.run_repeating(save_buttons_job, interval=300, first=300, name="buttons")
        if cfg.site_hours > 0:
            application.job_queue.run_repeating(sites_job, interval=cfg.site_hours * 3600, first=900, name="seiten")
        load_buttons()
        if cfg.disk_warn_gb > 0:
            application.job_queue.run_repeating(disk_job, interval=3600, first=120, name="speicher")
        if cfg.monitor_minutes > 0:
            application.job_queue.run_repeating(monitor_job, interval=cfg.monitor_minutes * 60, first=60,
                                                name="ueberwachung")
        if cfg.update_repo:
            application.job_queue.run_repeating(update_job, interval=24 * 3600, first=300, name="update")
        if (old := version.first_start_of_version(cfg.data_dir)) and old != "neu":
            what = version.CHANGES.get(version.__version__, "")
            await tell_admins(f"🆕 Bot aktualisiert: {old} → {version.__version__}" + (f"\n{what}" if what else ""))
        try:
            n = await tools.import_history()
            if n:
                log.info("Nutzerprofile: %d frühere Anfragen übernommen", n)
        except Exception:  # noqa: BLE001 - never block the start
            log.exception("Nutzerprofile: Import der Anfragen fehlgeschlagen")
        extras = [x for x, on in (("SABnzbd", sab), ("TMDB", tmdb)) if on]
        log.info("Bot %s läuft mit %s / %s%s, Admin %s", version.__version__, cfg.llm.provider, cfg.llm.model,
                 f" + {', '.join(extras)}" if extras else "", cfg.admin_id)

    async def post_shutdown(_: Application) -> None:
        save_buttons()
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
    app.add_handler(CommandHandler(["stumm", "ruhe", "mute"], on_mute, filters=allowed))
    app.add_handler(CommandHandler(["laut", "unmute"], on_unmute, filters=allowed))
    app.add_handler(CommandHandler(["meldungen", "benachrichtigungen"], on_notify_cmd, filters=allowed))
    app.add_handler(CallbackQueryHandler(on_notify_button, pattern=r"^nt:"))
    app.add_handler(CommandHandler("nutzer", on_users, filters=is_admin))
    app.add_handler(CommandHandler("limit", on_limit, filters=is_admin))
    app.add_handler(CommandHandler(["statistik", "stats"], on_stats, filters=is_admin))
    app.add_handler(CommandHandler(["check", "apis"], on_check, filters=is_admin))
    app.add_handler(CommandHandler("version", on_version, filters=allowed))
    app.add_handler(CommandHandler(["mediathek", "medien", "aufraeumen"], on_media, filters=is_admin))
    app.add_handler(CommandHandler(["aktivitaet", "anfragen", "probleme"], on_activity, filters=is_admin))
    app.add_handler(CommandHandler(["regeln", "aufraeumregeln"], on_cleanup, filters=is_admin))
    app.add_handler(CommandHandler(["freigaben", "freigabe"], on_approvals, filters=is_admin))
    app.add_handler(CommandHandler(["rezepte", "werkzeuge"], on_recipes, filters=is_admin))
    app.add_handler(CommandHandler(["monatsbericht", "bericht"], on_monthly, filters=is_admin))
    app.add_handler(CommandHandler(["admin", "verwaltung", "uebersicht"], on_admin, filters=is_admin))
    app.add_handler(CommandHandler(["log", "protokoll"], on_log, filters=is_admin))
    app.add_handler(CallbackQueryHandler(on_health_button, pattern=r"^h:"))
    app.add_handler(CommandHandler("debug", on_debug, filters=is_admin))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_flow_button, pattern=r"^f:"))
    app.add_handler(CallbackQueryHandler(on_offer_button, pattern=r"^o:"))
    app.add_handler(CallbackQueryHandler(on_rating_button, pattern=r"^rt:"))
    app.add_handler(CallbackQueryHandler(on_downloads_button, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(on_user_button, pattern=r"^u:"))
    app.add_handler(CallbackQueryHandler(on_notice_button, pattern=r"^n:"))
    app.add_handler(CallbackQueryHandler(on_stats_button, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(on_profile_button, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(on_plex_button, pattern=r"^x:"))
    app.add_handler(CallbackQueryHandler(on_media_button, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_activity_button, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(on_cleanup_button, pattern=r"^c:"))
    app.add_handler(CallbackQueryHandler(on_approval_button, pattern=r"^r:"))
    app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^g:"))
    app.add_handler(MessageHandler(~allowed, on_unknown))
    app.add_handler(MessageHandler(allowed & filters.COMMAND, on_start))  # unknown command: show help
    app.add_error_handler(on_error)
    return app
