"""Admin: manage the whole library. Radarr/Sonarr and Plex side by side, rendered in code (no AI tokens).

Sections
  missing     in Radarr/Sonarr, wanted, but not downloaded (yet)
  not_in_plex downloaded, but Plex doesn't show it (scan missing, wrong library path)
  watched     seen completely by everybody with a linked Plex account -> can go
  unwatched   on the server for a long time, nobody has even started it -> can probably go
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .arr import ArrError
from .german import file_check

log = logging.getLogger(__name__)

SECTIONS = {
    "missing": "⏳ Fehlt noch",
    "not_in_plex": "❓ Geladen, aber nicht in Plex",
    "watched": "✅ Von allen gesehen",
    "unwatched": "💤 Lange da, nie geschaut",
    "not_german": "🗣 Nicht wirklich auf Deutsch",
}
MAX_LINES = 15
MAX_BUTTONS = 8


def _dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def size_text(b: float) -> str:
    gb = b / 2**30
    return (f"{gb / 1024:.1f} TB" if gb >= 1024 else f"{gb:.1f} GB" if gb >= 10 else f"{gb:.2f} GB").replace(".", ",")


@dataclass
class Title:
    kind: str  # movie | series
    arr_id: int
    title: str
    year: int | None
    size: float
    added: datetime | None
    monitored: bool
    files: int  # movie: 0/1, series: episode files
    wanted: int  # movie: 1 if released and wanted, series: monitored aired episodes
    ended: bool = True
    ext_id: int | None = None  # tmdb (movie) / tvdb (series)
    in_plex: bool = False
    complete_by: list[int] = field(default_factory=list)  # user ids who saw everything
    started_by: list[int] = field(default_factory=list)  # user ids who saw at least something
    last_viewed: datetime | None = None
    not_german: int = 0  # files whose audio track has no German (movie: 0/1, series: episodes)
    mislabelled: int = 0  # of those: the name says German
    mistagged: bool = False  # every file named German, no German-tagged track: probably only wrong tags

    @property
    def name(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title

    @property
    def service(self) -> str:
        return "radarr" if self.kind == "movie" else "sonarr"

    @property
    def missing(self) -> int:
        return max(0, self.wanted - self.files)


@dataclass
class Report:
    titles: list[Title]
    viewers: list[int]  # users with a linked Plex account
    plex_ok: bool
    errors: list[str] = field(default_factory=list)
    made: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def section(self, name: str, days: int = 180) -> list[Title]:
        t = self.titles
        if name == "missing":
            return sorted((x for x in t if x.monitored and x.missing), key=lambda x: (-x.missing, x.name))
        if name == "not_german":
            return sorted((x for x in t if x.not_german and not x.mistagged),
                          key=lambda x: (-x.mislabelled, -x.not_german, x.name))
        if name == "mistagged":
            return sorted((x for x in t if x.not_german and x.mistagged), key=lambda x: x.name)
        if not self.plex_ok:
            return []
        if name == "not_in_plex":
            return sorted((x for x in t if x.files and not x.in_plex), key=lambda x: x.name)
        if name == "watched":
            everyone = set(self.viewers)
            # Running series get new episodes: only finished ones count as "can go".
            return sorted((x for x in t if x.files and x.in_plex and everyone and everyone <= set(x.complete_by)
                           and (x.kind == "movie" or x.ended)), key=lambda x: -x.size)
        if name == "unwatched":
            cutoff = self.made - timedelta(days=days)
            return sorted((x for x in t if x.files and x.added and x.added < cutoff and not x.started_by),
                          key=lambda x: -x.size)
        raise ValueError(name)


async def _series_languages(sonarr: Any, series: list[Title], errors: list[str]) -> None:
    """Real audio language of every episode file (one quick Sonarr call per series)."""
    sem = asyncio.Semaphore(4)

    async def one(t: Title) -> None:
        async with sem:
            try:
                files = await sonarr.episode_files(t.arr_id)
            except ArrError as e:
                errors.append(f"{t.title}: {e}")
                return
        checks = [file_check(f) for f in files]
        t.not_german = sum(c in ("other", "mislabelled") for c in checks)
        t.mislabelled = checks.count("mislabelled")
        t.mistagged = t.mislabelled >= 2 and t.mislabelled == t.not_german and "german" not in checks

    await asyncio.gather(*(one(t) for t in series))


async def collect(radarr: Any, sonarr: Any, plex_client: Any, accounts: list[Any],
                  german_check: bool = True) -> Report:
    titles: list[Title] = []
    errors: list[str] = []
    if radarr:
        try:
            for m in await radarr.library():
                t = Title(
                    "movie", m["id"], m.get("title", "?"), m.get("year"), m.get("sizeOnDisk") or 0,
                    _dt(m.get("added")), bool(m.get("monitored")), 1 if m.get("hasFile") else 0,
                    1 if m.get("isAvailable", True) else 0, True, m.get("tmdbId"))
                if m.get("hasFile") and german_check:
                    check = file_check(m.get("movieFile"))
                    t.not_german = int(check in ("other", "mislabelled"))
                    t.mislabelled = int(check == "mislabelled")
                titles.append(t)
        except ArrError as e:
            errors.append(str(e))
    if sonarr:
        try:
            for s in await sonarr.library():
                st = s.get("statistics") or {}
                titles.append(Title(
                    "series", s["id"], s.get("title", "?"), s.get("year"), st.get("sizeOnDisk") or 0,
                    _dt(s.get("added")), bool(s.get("monitored")), st.get("episodeFileCount") or 0,
                    st.get("episodeCount") or 0, s.get("status") == "ended", s.get("tvdbId")))
        except ArrError as e:
            errors.append(str(e))
        if german_check:
            await _series_languages(sonarr, [t for t in titles if t.kind == "series" and t.files], errors)

    plex_ok = False
    by_key = {(t.kind, t.ext_id): t for t in titles if t.ext_id}
    for a in accounts:
        try:
            items = await plex_client.library(a)
        except Exception as e:  # noqa: BLE001 - one broken account must not hide the rest
            errors.append(f"Plex ({a.username}): {e}")
            continue
        plex_ok = True
        for it in items:
            t = by_key.get((it.kind, it.tmdb if it.kind == "movie" else it.tvdb))
            if not t:
                continue
            t.in_plex = True
            if it.complete:
                t.complete_by.append(a.user_id)
            if it.watched or it.started:
                t.started_by.append(a.user_id)
            if it.last_viewed:
                seen = datetime.fromtimestamp(it.last_viewed, timezone.utc)
                t.last_viewed = max(filter(None, (t.last_viewed, seen)))
    viewers = [a.user_id for a in accounts]
    return Report(titles, viewers, plex_ok, errors)


# ---- texts ------------------------------------------------------------------------------------
def render_overview(r: Report, days: int, name_of: Callable[[int], str]) -> tuple[str, list[tuple[str, str]]]:
    movies = [t for t in r.titles if t.kind == "movie" and t.files]
    series = [t for t in r.titles if t.kind == "series" and t.files]
    total = sum(t.size for t in r.titles)
    out = ["🗂 <b>Mediathek-Check</b>",
           f"🎬 {len(movies)} Filme · 📺 {len(series)} Serien · {size_text(total)} belegt"]
    if r.plex_ok:
        out.append("👥 Plex-Abgleich mit: " + html.escape(", ".join(name_of(u) for u in r.viewers)))
    elif r.viewers:
        out.append("⚠️ Plex gerade nicht erreichbar – nur Radarr/Sonarr.")
    else:
        out.append("ℹ️ Ohne Plex-Verknüpfung sehe ich nicht, was geschaut wurde: /plex")
    out.append("")
    buttons = []
    for key, label in SECTIONS.items():
        if key not in ("missing", "not_german") and not r.plex_ok:
            continue
        items = r.section(key, days)
        size = sum(t.size for t in items)
        extra = f" · {size_text(size)}" if key in ("watched", "unwatched") and size else ""
        what = f" (seit über {days} Tagen)" if key == "unwatched" else ""
        unsure = len(r.section("mistagged")) if key == "not_german" else 0
        if unsure:
            extra += f" · ❔ {unsure} vermutlich nur falsch markiert"
        out.append(f"{label}{what}: <b>{len(items)}</b>{extra}")
        if items or unsure:
            buttons.append((f"{label.split(' ', 1)[0]} {len(items)} anzeigen", f"m:{key}"))
    for e in r.errors[:3]:
        out.append(f"⚠️ {html.escape(e[:150])}")
    out.append(f"\n<i>Stand {r.made.astimezone():%H:%M} Uhr</i>")
    buttons.append(("🔄 Neu prüfen", "m:r"))
    return "\n".join(out), buttons


def render_section(r: Report, key: str, days: int, name_of: Callable[[int], str],
                   can_listen: bool = False) -> tuple[str, list[tuple[str, str]]]:
    items = r.section(key, days)
    e = lambda s: html.escape(str(s), quote=False)  # noqa: E731
    title = SECTIONS[key] + (f" (seit über {days} Tagen)" if key == "unwatched" else "")
    out = [f"<b>{title}</b> – {len(items)}"]
    if not items:
        out.append("\nNichts dabei. 👍")
    for t in items[:MAX_LINES]:
        icon = "🎬" if t.kind == "movie" else "📺"
        if key == "missing":
            what = "noch nicht geladen" if t.kind == "movie" else f"{t.missing} von {t.wanted} Folgen fehlen"
            out.append(f"{icon} {e(t.name)} – {what}")
        elif key == "not_german":
            if t.kind == "movie":
                what = "heißt Deutsch, Tonspur nicht" if t.mislabelled else "keine deutsche Tonspur"
            else:
                what = f"{t.not_german} von {t.files} Folgen ohne deutsche Tonspur" + (
                    f", {t.mislabelled} davon heißen Deutsch" if t.mislabelled else "")
            out.append(f"{icon} {e(t.name)} – {what}")
        elif key == "not_in_plex":
            out.append(f"{icon} {e(t.name)} · {size_text(t.size)}")
        elif key == "watched":
            when = f" · zuletzt {t.last_viewed:%d.%m.%Y}" if t.last_viewed else ""
            out.append(f"{icon} {e(t.name)} · {size_text(t.size)}{when}")
        else:
            since = f" · da seit {t.added:%m/%Y}" if t.added else ""
            out.append(f"{icon} {e(t.name)} · {size_text(t.size)}{since}")
    if len(items) > MAX_LINES:
        out.append(f"<i>… und {len(items) - MAX_LINES} weitere</i>")

    buttons: list[tuple[str, str]] = []
    if key == "missing" and items:
        out.append("\n<i>Meist gibt es noch keinen passenden Download (z. B. nicht auf Deutsch).</i>")
        buttons.append(("🔍 Alle fehlenden suchen", "m:search"))
    elif key == "not_german":
        unsure = r.section("mistagged")
        if unsure:
            out.append(f"\n<b>❔ Vermutlich nur falsch markiert – {len(unsure)}</b>")
            out += [f"{'🎬' if t.kind == 'movie' else '📺'} {e(t.name)} – alle {t.files} Folgen heißen Deutsch, "
                    "keine Tonspur ist als Deutsch markiert" for t in unsure[:MAX_LINES]]
            out.append("<i>Alte DVD-Rips markieren die deutsche Tonspur oft als Englisch. "
                       + ("„🎧 Reinhören“: ich höre ein paar kurze Stellen an (ca. 0,2 ct pro Stelle) und merke mir "
                          "das Ergebnis." if can_listen else
                          "Hör kurz rein – ist es Deutsch, tipp „✅ Ist Deutsch“ und ich melde den Titel nie wieder.")
                       + "</i>")
        if items:
            out.append("\n<i>Geprüft wird die Tonspur-Markierung, die Radarr/Sonarr beim Import auslesen – nicht der "
                       "Release-Name. 🇩🇪 antippen: ich suche ein anderes deutsches Release und frage vor dem "
                       "Ersetzen. Stimmt es doch: „✅ Ist Deutsch“.</i>")
        buttons += [(f"🇩🇪 {t.name[:34]}", f"m:gf:{t.kind[0]}:{t.ext_id}") for t in items[:MAX_BUTTONS] if t.ext_id]
        check = unsure + [t for t in items if t.mislabelled or can_listen]
        if can_listen:
            buttons += [(f"🎧 Reinhören: {t.name[:30]}", f"m:hear:{t.kind[0]}:{t.arr_id}") for t in check[:MAX_BUTTONS]]
        else:
            buttons += [(f"✅ Ist Deutsch: {t.name[:28]}", f"m:ok:{t.kind[0]}:{t.arr_id}") for t in check[:MAX_BUTTONS]]
    elif key == "not_in_plex" and items:
        out.append("\n<i>Oft fehlt nur ein Scan in Plex. Bleibt es danach, stimmt meist der Bibliotheks-Pfad "
                   "in Plex nicht mit dem von Radarr/Sonarr überein.</i>")
        buttons.append(("🔄 Plex-Bibliotheken scannen", "m:scan"))
    elif key in ("watched", "unwatched") and items:
        size = sum(t.size for t in items)
        out.append(f"\nZusammen {size_text(size)}. Antippen zum Löschen (ich frage vorher nach):")
        buttons += [(f"🗑 {t.name[:28]} · {size_text(t.size)}", f"n:rm:{t.service}:{t.arr_id}")
                    for t in items[:MAX_BUTTONS]]
        if key == "watched":
            out.append("<i>Gezählt wird nur, wer sein Plex verknüpft hat: "
                       + e(", ".join(name_of(u) for u in r.viewers)) + "</i>")
    buttons.append(("⬅️ Übersicht", "m:o"))
    return "\n".join(out)[:4096], buttons
