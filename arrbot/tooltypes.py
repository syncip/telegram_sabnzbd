"""Data classes, constants and small helpers shared by the tool modules (no ToolRunner)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import json
import logging
from typing import Any

from .arr import ArrError

log = logging.getLogger(__name__)


MAX_RESULTS = 5
MAX_SEASON_BUTTONS = 10
MAX_SUGGESTIONS = 6
MAX_TOP = 10
NO_TMDB = 'ℹ️ Dafür brauche ich einen kostenlosen TMDB-Key. Der Admin trägt ihn in der docker-compose.yml als TMDB_API_KEY ein (themoviedb.org → Einstellungen → API).'
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
          "Oktober", "November", "Dezember"]


@dataclass
class View:
    """A message the bot renders in code (downloads, statistics, users, API check)."""

    chat_id: int
    kind: str  # downloads | stats | users | check | profile
    finished: bool = False
    query: str | None = None  # downloads: only titles containing this
    arg: Any = None  # calendar: (days, mine)


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", False, [], {})}


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Step:
    """What the bot shows next for a flow: a question with buttons, or a final text."""

    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (label, value)
    timeout: int = 0  # seconds until the default is taken (quality only)
    done: bool = False
    rows: list[int] = field(default_factory=list)  # buttons per row (e.g. [4, 4, 2, 1]); empty = one per row


@dataclass
class Candidate:
    kind: str  # "movie" | "series"
    ext_id: int  # tmdb (movie) or tvdb (series)
    title: str
    year: int | None
    seasons: list[int] = field(default_factory=list)
    lib_id: int | None = None
    has_file: bool = False
    rating: float | None = None  # set for suggestions
    tmdb_id: int | None = None  # suggestions: for "nichts davon"
    genres: list[str] = field(default_factory=list)  # suggestions: German genre names

    @property
    def label(self) -> str:
        s = ("🎬 " if self.kind == "movie" else "📺 ") if self.rating is not None else ""
        s += f"{self.title} ({self.year})" if self.year else self.title
        if self.kind == "series" and self.seasons:
            s += f" · {len(self.seasons)} St."
        if self.rating:
            s += f" · ⭐ {self.rating:.1f}".replace(".", ",")
        return s


@dataclass
class Flow:
    id: str
    chat_id: int
    cand: Candidate
    monitor: str | None = None
    seasons: list[int] | None = None
    profile: str | None = None
    stage: str = ""
    profiles: list[str] = field(default_factory=list)
    default_profile: str = ""
    message_id: int | None = None
    step: Step | None = None
    missing: list[int] = field(default_factory=list)  # extend stage: seasons not (fully) downloaded
    data: Any = None  # gfix / clean stage: the plan to run on confirmation


@dataclass
class Offer:
    id: str
    chat_id: int
    cands: list[Candidate]
    message_id: int | None = None
    text: str = "🔎 Welchen meinst du?"
    bulk: bool = False  # top lists: extra button "add all"
    suggest: bool = False  # suggestions: extra button "none of these" (learned)
    calibrate: bool = False  # quick start: 👍/👎 per title, then "Fertig"
    mode: str = ""  # "rate": buttons 👎/👁 per title instead of "add"
    hidden: list[int] = field(default_factory=list)  # titles already rated (button gone)


def DownloadsView(chat_id: int, finished: bool = False) -> "View":  # noqa: N802 - kept for readability
    return View(chat_id, "downloads", finished)


class Held(ArrError):
    """The wish waits for the admin's approval (not an error for the user)."""


@dataclass
class Final:
    """Result of an action tool: the turn ends here, without another AI call."""

    text: str


def _year_filter(res: list[dict], year: int | None) -> list[dict]:
    if not year:
        return res
    close = [m for m in res if m.get("year") and abs(m["year"] - year) <= 1]
    return close or res


def _movie_cand(m: dict) -> Candidate:
    return Candidate("movie", m.get("tmdbId"), m.get("title", "?"), m.get("year") or None,
                     lib_id=m.get("id") or None, has_file=bool(m.get("hasFile")))


def _series_cand(s: dict) -> Candidate:
    seasons = sorted(x["seasonNumber"] for x in s.get("seasons", []) if x.get("seasonNumber", 0) > 0)
    stats = s.get("statistics") or {}
    return Candidate("series", s.get("tvdbId"), s.get("title", "?"), s.get("year") or None, seasons,
                     lib_id=s.get("id") or None,
                     has_file=bool(stats.get("episodeCount")) and stats.get("episodeFileCount") == stats.get("episodeCount"))


def _season_buttons(seasons: list[int]) -> list[tuple[str, str]]:
    shown = seasons if len(seasons) <= MAX_SEASON_BUTTONS else seasons[:3]
    buttons = [("Alle Staffeln", "all")] + [(f"Staffel {n}", f"s{n}") for n in shown]
    if len(seasons) > MAX_SEASON_BUTTONS:
        buttons.append((f"Neueste (Staffel {seasons[-1]})", f"s{seasons[-1]}"))
    buttons.append(("Nur neue Folgen", "future"))
    return buttons


def _what(flow: Flow) -> str:
    c = flow.cand
    if c.kind == "series":
        if flow.seasons:
            return f"{c.title} (Staffel {', '.join(map(str, flow.seasons))})"
        if flow.monitor == "future":
            return f"{c.title} (nur neue Folgen)"
    return c.label if c.kind == "movie" else c.title


def queue_title(q: dict) -> str:
    if "movie" in q and q["movie"]:
        return f"{q['movie'].get('title')} ({q['movie'].get('year')})"
    if "series" in q and q["series"]:
        ep = q.get("episode") or {}
        if ep:
            return f"{q['series'].get('title')} S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
        return q["series"].get("title")
    return q.get("title", "?")


def _season_overview(series: dict) -> tuple[list[int], list[int]]:
    """(complete seasons, seasons with missing episodes) – specials (0) ignored."""
    have, missing = [], []
    for x in sorted(series.get("seasons") or [], key=lambda x: x.get("seasonNumber", 0)):
        n = x.get("seasonNumber", 0)
        if n <= 0:
            continue
        st = x.get("statistics") or {}
        files, total = st.get("episodeFileCount", 0), st.get("totalEpisodeCount", 0)
        if total and files >= total:
            have.append(n)
        else:
            missing.append(n)
    return have, missing


def _seasons_text(seasons: list[int], short: bool = False) -> str:
    """[1, 2, 3, 5] -> 'Staffel 1–3, 5'."""
    if not seasons:
        return ""
    parts: list[str] = []
    start = prev = seasons[0]
    for n in seasons[1:] + [None]:  # type: ignore[list-item]
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}–{prev}" if prev - start >= 2 else ", ".join(map(str, range(start, prev + 1))))
        if n is not None:
            start = prev = n
    return ("St. " if short else "Staffel ") + ", ".join(parts)


def release_languages(releases: list[dict]) -> dict:
    """How many indexer releases exist, and how many of them are German."""

    def german(r: dict) -> bool:
        langs = [(lng.get("name") or "").lower() for lng in r.get("languages") or []]
        if isinstance(r.get("language"), dict):  # Sonarr v3
            langs.append((r["language"].get("name") or "").lower())
        title = (r.get("title") or "").lower().replace(" ", ".")
        return "german" in langs or ".german." in f".{title}."

    return {"releases": len(releases), "deutsch": sum(1 for r in releases if german(r))}


def mute(store: Any, chat_id: int, minutes: int | None) -> str:
    """Mute download messages for this chat (minutes) or switch them on again (None)."""
    from datetime import datetime, timezone

    muted = store.state.muted
    key = str(chat_id)
    if minutes is None:
        m = muted.pop(key, None)
        store.save()
        if not m:
            return "🔔 Meldungen waren nicht stummgeschaltet."
        n = m.get("missed", 0)
        return "🔔 Meldungen sind wieder an." + (
            f" In der Zeit gab es {n} Meldung{'en' if n != 1 else ''} – Überblick: /downloads" if n else "")
    minutes = max(1, min(int(minutes), 7 * 24 * 60))
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    old = muted.get(key) or {}
    muted[key] = {"until": until.isoformat(), "missed": old.get("missed", 0), "lines": old.get("lines", [])}
    store.save()
    if minutes < 120:
        span = f"{minutes} Minuten"
    elif minutes < 48 * 60:
        span = f"{minutes / 60:.0f} Stunden"
    else:
        span = f"{minutes / 1440:.0f} Tage"
    return (f"🔕 Stumm für {span}. Download-Meldungen sammle ich und sage dir danach kurz, was war. "
            "Früher wieder an: „Meldungen wieder an“ oder /laut.")


async def resume_search(svc: Any, item_id: int) -> None:
    """Monitor again and start a search right away (used by resume and by the tracker after a pause)."""
    await svc.set_monitored(item_id, True)
    await svc.search(item_id)
