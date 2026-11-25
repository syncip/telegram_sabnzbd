"""'Sag mir, wenn es auf Deutsch kommt' and the weekly overview. Runs in code, once a day.

Movies: TMDB dates for Germany (digital / disc / streaming). As soon as a German home release exists,
the bot starts the Radarr search (title in the library) or offers to add it.
Series: one indexer search per watched season and day. New German episodes are offered with the
"auf Deutsch laden" buttons (same as german_fix).
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from . import manage
from .arr import ArrError
from .german import _air, file_check
from .tmdb import TmdbError

if TYPE_CHECKING:
    from .tools import ToolRunner

log = logging.getLogger(__name__)

DAYS = ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"]


@dataclass
class Notice:
    chat_id: int
    text: str
    outbox: bool = False  # the ToolRunner outbox of chat_id holds buttons to send after the text
    done: bool = False  # the watch is finished


def _d(d: date) -> str:
    return d.strftime("%d.%m.%Y")


async def check_movie(tools: "ToolRunner", w: dict, today: date) -> Notice | None:
    chat, tmdb_id, title = w["user_id"], w["ext_id"], w["title"]
    info = await tools.tmdb.german_info("movie", tmdb_id)
    home = min(filter(None, (info.digital, info.disc)), default=None)
    available = bool(info.streaming) or (home is not None and home <= today)
    movie = await tools.radarr.lookup_tmdb(tmdb_id) if tools.radarr else {}
    lib_id = movie.get("id")
    if lib_id:
        full = await tools.radarr.get(lib_id)
        if full.get("hasFile") and file_check(full.get("movieFile")) == "german":
            return Notice(chat, f"✅ {title} hast du schon auf Deutsch – ich beobachte es nicht mehr.", done=True)
    if not available:
        return None
    since = f"digital seit {_d(home)}" if home and home <= today else f"im Stream bei {', '.join(info.streaming[:2])}"
    if lib_id:
        await tools.radarr.search(lib_id)
        tools.store.track("radarr", lib_id, title, chat)
        return Notice(chat, f"🇩🇪 {title} gibt es jetzt auf Deutsch ({since}). Ich habe die Suche gestartet und "
                            "melde mich, wenn der Download läuft.", done=True)
    cand = await tools._candidate(chat, "movie", tmdb_id)
    tools._offer(chat, [cand], f"🇩🇪 {title} gibt es jetzt auf Deutsch ({since}). Hinzufügen?")
    return Notice(chat, "", outbox=True, done=True)


async def check_series(tools: "ToolRunner", w: dict, today: date) -> Notice | None:
    chat, tvdb_id, title = w["user_id"], w["ext_id"], w["title"]
    series = await tools.sonarr.lookup_tvdb(tvdb_id)
    lib_id = series.get("id")
    if not lib_id:
        return Notice(chat, f"ℹ️ {title} ist nicht mehr in Sonarr – ich beobachte es nicht mehr.", done=True)
    season = w["season"]
    if season is None:
        aired = [e["seasonNumber"] for e in await tools.sonarr.episodes(lib_id)
                 if e.get("seasonNumber", 0) > 0 and (_air(e) or date.max) <= today]
        season = max(aired, default=None)
        if season is None:
            return None
    plan = await manage.plan_german(tools.sonarr, lib_id, season, today)
    if not plan.missing and not plan.other_lang:
        if plan.checked and (w["season"] is not None or series.get("status") == "ended"):
            return Notice(chat, f"✅ {title} Staffel {season} hast du komplett auf Deutsch – fertig beobachtet.",
                          done=True)
        return None
    covered = sorted({e["id"] for g in plan.grabs for e in g.episodes})
    if len(covered) <= w["known"]:
        return None
    tools.profiles.update_watch(w["id"], len(covered))
    text, buttons = manage.render_plan(plan)
    text = f"🔔 Neue deutsche Folgen von {title}!\n\n" + text
    await tools._series_job(chat, tvdb_id, "gfix", text, buttons, plan)
    return Notice(chat, "", outbox=True)


async def check_watches(tools: "ToolRunner", today: date | None = None, only: list[dict] | None = None) -> list[Notice]:
    """All watches (daily job) or the given ones (right after adding)."""
    today = today or date.today()
    out: list[Notice] = []
    for w in only if only is not None else tools.profiles.watches():
        try:
            if w["kind"] == "movie":
                if not tools.tmdb:
                    continue
                n = await check_movie(tools, w, today)
            else:
                if not tools.sonarr:
                    continue
                n = await check_series(tools, w, today)
        except (ArrError, TmdbError) as e:
            log.warning("Deutsch-Beobachtung %s: %s", w["title"], e)
            continue
        if n is None:
            tools.profiles.update_watch(w["id"], w["known"])
            continue
        if n.done:
            tools.profiles.drop_watch(w["id"])
        out.append(n)
    return out


# ---- weekly overview -------------------------------------------------------------------------
@dataclass
class Digest:
    text: str
    suggestions: bool = False


async def weekly_digest(tools: "ToolRunner", chat: int, tz: Any, today: date | None = None) -> Digest:
    from . import extras

    today = today or date.today()
    out = ["📰 <b>Dein Wochenrückblick</b>"]
    week_ago = today - timedelta(days=7)
    done = [f["title"] for f in tools.store.state.finished
            if chat in (f.get("chat_ids") or []) and f.get("at", "")[:10] >= week_ago.isoformat()]
    if done:
        out.append(f"\n<b>✅ Fertig geladen ({len(done)})</b>")
        out += [f"• {html.escape(t)}" for t in done[:10]] + ([f"<i>… und {len(done) - 10} weitere</i>"] if len(done) > 10 else [])
    mine = {(r["kind"], r.get("tmdb") if r["kind"] == "movie" else r.get("tvdb"))
            for r in tools.store.state.requests if r["chat_id"] == chat}
    entries = await extras.calendar_entries(tools.radarr, tools.sonarr, today, today + timedelta(days=6), tz, mine)
    if entries:
        out.append("\n<b>📅 Kommt diese Woche</b>")
        out += [f"{extras.WEEKDAYS[e.day.weekday()]} {e.day:%d.%m.} {html.escape(e.text)}" for e in entries[:12]]
    watches = tools.profiles.watches(chat) if tools.profiles else []
    if watches:
        out.append(f"\n🔔 Ich warte bei {len(watches)} Titeln auf die deutsche Fassung: "
                   + html.escape(", ".join(w["title"] for w in watches[:5])))
    if len(out) == 1:
        out.append("\nDiese Woche war ruhig. 😴")
    out.append("\n<i>Abbestellen: „Keinen Wochenrückblick mehr“</i>")
    return Digest("\n".join(out), suggestions=tools.tmdb is not None)


def parse_day(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit()):
        n = int(raw)
        return n if 0 <= n <= 6 else None
    s = str(raw).strip().lower()
    for i, name in enumerate(DAYS):
        if s.startswith(name[:2]):
            return i
    return None
