"""Calendar ("Was kommt diese Woche?") and disk space, rendered in code (no AI tokens)."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

from .arr import ArrError
from .downloads import bar

log = logging.getLogger(__name__)

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MAX_PER_DAY = 8


def _dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass
class Entry:
    day: date
    kind: str  # movie | series
    text: str
    sort: str = ""
    have: bool = False


async def calendar_entries(radarr: Any, sonarr: Any, start: date, end: date, tz: tzinfo,
                           only: set[tuple[str, int]] | None = None) -> list[Entry]:
    """Episodes airing and movies released (cinema/digital/disc) between start and end (inclusive).
    only: {("movie", tmdb), ("series", tvdb)} to show just one person's titles."""
    out: list[Entry] = []
    if sonarr:
        try:
            for ep in await sonarr.calendar(start, end + timedelta(days=1)):
                series = ep.get("series") or {}
                if only is not None and ("series", series.get("tvdbId")) not in only:
                    continue
                when = _dt(ep.get("airDateUtc"))
                if not when:
                    continue
                local = when.astimezone(tz)
                if not start <= local.date() <= end:
                    continue
                label = f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
                name = series.get("title") or "?"
                title = ep.get("title")
                text = f"📺 {name} {label}" + (f" „{title}“" if title and title.upper() != "TBA" else "")
                out.append(Entry(local.date(), "series", text + f" · {local:%H:%M}", f"{local:%H%M}{name}",
                                 bool(ep.get("hasFile"))))
        except ArrError as e:
            log.warning("Sonarr-Kalender: %s", e)
    if radarr:
        try:
            for m in await radarr.calendar(start, end + timedelta(days=1)):
                if only is not None and ("movie", m.get("tmdbId")) not in only:
                    continue
                for field, what in (("inCinemas", "im Kino"), ("digitalRelease", "digital"), ("physicalRelease", "auf Disc")):
                    when = _dt(m.get(field))
                    if when and start <= when.date() <= end:
                        name = f"{m.get('title')} ({m.get('year')})" if m.get("year") else m.get("title", "?")
                        out.append(Entry(when.date(), "movie", f"🎬 {name} – {what}", f"9999{name}",
                                         bool(m.get("hasFile"))))
        except ArrError as e:
            log.warning("Radarr-Kalender: %s", e)
    return sorted(out, key=lambda e: (e.day, e.sort))


def render_calendar(entries: list[Entry], start: date, end: date, today: date, mine: bool) -> str:
    e = lambda s: html.escape(s, quote=False)  # noqa: E731
    span = "diese Woche" if (end - start).days <= 7 and start <= today else f"{start:%d.%m.} – {end:%d.%m.}"
    out = [f"📅 <b>{'Deine Titel' if mine else 'Was kommt'} {e(span)}</b>"]
    if not entries:
        out.append("\nIn dem Zeitraum kommt nichts Neues." + (" Frag nach „alle“, um alles zu sehen." if mine else ""))
        return "\n".join(out)
    day = None
    count = 0
    for x in entries:
        if x.day != day:
            day, count = x.day, 0
            label = "Heute" if day == today else ("Morgen" if day == today + timedelta(days=1) else WEEKDAYS[day.weekday()])
            out.append(f"\n<b>{label} {day:%d.%m.}</b>")
        count += 1
        if count == MAX_PER_DAY + 1:
            out.append("   <i>… und mehr</i>")
        if count > MAX_PER_DAY:
            continue
        out.append(e(x.text) + (" ✅" if x.have else ""))
    out.append("\n<i>Sendezeiten im Original; Filmtermine laut Radarr (meist USA). ✅ = schon da</i>")
    return "\n".join(out)[:4096]


# ---- disk space ------------------------------------------------------------------------------
@dataclass
class Disk:
    path: str
    free: float  # bytes
    total: float

    @property
    def used_pct(self) -> float:
        return 100 * (1 - self.free / self.total) if self.total else 0.0


def _size(b: float) -> str:
    tb = b / 2**40
    return (f"{tb:.1f} TB" if tb >= 1 else f"{b / 2**30:.0f} GB").replace(".", ",")


async def disks(radarr: Any, sonarr: Any, chaptarr: Any = None) -> list[Disk]:
    """The drives that hold the media (and book) root folders (one entry per drive)."""
    found: dict[str, Disk] = {}
    for svc in (radarr, sonarr, chaptarr):
        if not svc:
            continue
        try:
            roots = await svc.root_folders()
            space = await svc.diskspace()
        except ArrError as e:
            log.warning("Speicherplatz: %s", e)
            continue
        for r in roots:
            path = r.get("path") or ""
            # The mount that contains the root folder: the longest matching path.
            mounts = [d for d in space if path.startswith(d.get("path") or "\0")]
            d = max(mounts, key=lambda d: len(d.get("path") or ""), default=None)
            if d and d.get("totalSpace"):
                found.setdefault(d["path"], Disk(path, d.get("freeSpace") or 0, d.get("totalSpace") or 0))
            elif r.get("freeSpace") is not None:
                found.setdefault(path, Disk(path, r.get("freeSpace") or 0, r.get("totalSpace") or 0))
    return list(found.values())


def render_disks(ds: list[Disk], warn_gb: int, cleanup: list[str] | None = None) -> str:
    if not ds:
        return "💾 Speicherplatz konnte ich nicht abfragen."
    out = ["💾 <b>Speicherplatz</b>"]
    for d in ds:
        low = warn_gb and d.free < warn_gb * 2**30
        out.append(f"\n<b>{html.escape(d.path)}</b>" + (" ⚠️" if low else ""))
        if d.total:
            out.append(f"<code>{bar(d.used_pct)}</code> {d.used_pct:.0f} % belegt")
            out.append(f"{_size(d.free)} frei von {_size(d.total)}")
        else:
            out.append(f"{_size(d.free)} frei")
    if cleanup:
        out.append("\n<b>🧹 Könnte weg (von allen gesehen, größte zuerst)</b>")
        out += [html.escape(x) for x in cleanup]
        out.append("<i>Löschen einfach per Nachricht: „Lösch …“</i>")
    return "\n".join(out)


def low_disks(ds: list[Disk], warn_gb: int) -> list[Disk]:
    return [d for d in ds if warn_gb and d.free < warn_gb * 2**30]
