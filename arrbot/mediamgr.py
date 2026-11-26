"""🗂 Media manager for the admin: "lösche alle Filme, die länger als 180 Tage da sind und die ich nicht
gesehen habe", "lösche alle gesehenen Filme", "was frisst am meisten Platz?".

The AI only fills in a Query. Which titles match is decided here, in code, from Radarr/Sonarr and the
admin's own Plex history ("gesehen" = the admin saw it). Nothing is deleted without a preview and a tap.
Deleting goes straight through Radarr/Sonarr with the files, but without an import exclusion: anything
deleted can be added again any time. Episodes and seasons are deleted as files and stop being monitored,
so Sonarr does not load them again by itself ("such nochmal" brings them back).

Protected, never in a delete list: titles marked "🛡 behalten", titles someone else is in the middle of
(watched a bit within PROTECT_DAYS), and titles someone else requested within PROTECT_DAYS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .arr import ArrError
from .media import Report, Title, _dt, size_text
from .profiles import genre_de

log = logging.getLogger(__name__)

PROTECT_DAYS = 30
SCOPES = {"episodes": "einzelne Folgen", "seasons": "ganze Staffeln", "series": "ganze Serien"}
PREVIEW_LINES = 20


@dataclass
class Query:
    kind: str = "all"  # movie | series | all
    watched: bool | None = None  # True: the admin saw it completely; False: never started it
    older_days: int | None = None  # on the server for more than N days
    seen_days: int | None = None  # last watched more than N days ago
    min_gb: float | None = None
    max_rating: float | None = None
    genres: list[str] = field(default_factory=list)  # German names
    ended: bool | None = None  # series: only ended ones
    scope: str = ""  # series: episodes | seasons | series ("" = ask)
    largest: int | None = None  # only the N biggest

    def text(self) -> str:
        what = {"movie": "Filme", "series": "Serien", "all": "Filme und Serien"}[self.kind]
        parts = [what]
        if self.watched is True:
            parts.append("von dir gesehen")
        elif self.watched is False:
            parts.append("von dir nie angefangen")
        if self.older_days:
            parts.append(f"länger als {self.older_days} Tage da")
        if self.seen_days:
            parts.append(f"zuletzt vor über {self.seen_days} Tagen geschaut")
        if self.min_gb:
            parts.append(f"ab {self.min_gb:g} GB".replace(".", ","))
        if self.max_rating:
            parts.append(f"Bewertung bis {self.max_rating:g}".replace(".", ","))
        if self.genres:
            parts.append(", ".join(self.genres))
        if self.ended:
            parts.append("beendet")
        if self.scope and self.kind != "movie":
            parts.append(f"Serien: {SCOPES[self.scope]}")
        if self.largest:
            parts.append(f"die {self.largest} größten")
        return " · ".join(parts)


@dataclass
class Item:
    """One thing to delete: a movie, a series, a season or some episodes of a series."""

    what: str  # movie | series | season | episodes
    arr_id: int
    name: str
    size: float
    file_ids: list[int] = field(default_factory=list)
    episode_ids: list[int] = field(default_factory=list)
    service: str = "radarr"

    @property
    def icon(self) -> str:
        return "🎬" if self.what == "movie" else "📺"


@dataclass
class Job:
    """A delete list waiting for the admin: everything at once or one by one."""

    query: Query
    items: list[Item] = field(default_factory=list)
    protected: list[tuple[str, str]] = field(default_factory=list)  # (name, why)
    pending_series: list[Title] = field(default_factory=list)  # waiting for the scope question
    pos: int = 0
    deleted: list[Item] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # 🛡 never delete
    skipped: int = 0


def _norm_genres(names: list[str]) -> set[str]:
    return {g for g in (genre_de(x) for x in names) if g}


def select(report: Report, q: Query, admin_id: int | None, kept: set[tuple[str, int]],
           recent_requests: dict[tuple[str, int], int], name_of: Callable[[int], str] | None = None,
           now: datetime | None = None) -> tuple[list[Title], list[tuple[str, str]]]:
    """Titles matching the query (largest first) and the ones held back with the reason.
    recent_requests: (kind, ext id) -> user id of a request within PROTECT_DAYS."""
    now = now or datetime.now(timezone.utc)
    name_of = name_of or (lambda uid: str(uid))
    want_genres = _norm_genres(q.genres)
    hits: list[Title] = []
    protected: list[tuple[str, str]] = []
    series_parts = q.kind != "movie" and q.scope in ("episodes", "seasons")
    for t in report.titles:
        if not t.files or (q.kind != "all" and t.kind != q.kind):
            continue
        if q.ended and t.kind == "series" and not t.ended:
            continue
        if q.older_days and (not t.added or t.added > now - timedelta(days=q.older_days)):
            continue
        if q.max_rating and (t.rating is None or t.rating > q.max_rating):
            continue
        if want_genres and not want_genres & _norm_genres(t.genres):
            continue
        if q.min_gb and t.size < q.min_gb * 2**30 and not (t.kind == "series" and series_parts):
            continue
        # Watched state: whole title for movies and whole series; parts are checked per episode later.
        if not (t.kind == "series" and series_parts):
            if q.watched is True and admin_id not in t.complete_by:
                continue
            if q.watched is False and admin_id in t.started_by:
                continue
            if q.seen_days:
                last = t.viewed_by.get(admin_id) if admin_id is not None else None
                if not last or last > now - timedelta(days=q.seen_days):
                    continue
        elif q.watched is True and admin_id not in t.started_by:
            continue  # parts of a series the admin never touched cannot be "seen"
        if (t.service, t.arr_id) in kept:
            protected.append((t.name, "🛡 behalten"))
            continue
        watcher = next((u for u, seen in t.viewed_by.items()
                        if u != admin_id and u in t.started_by and u not in t.complete_by
                        and seen > now - timedelta(days=PROTECT_DAYS)), None)
        if watcher is not None:
            protected.append((t.name, f"👀 {name_of(watcher)} schaut es gerade"))
            continue
        asker = recent_requests.get((t.kind, t.ext_id or 0))
        if asker is not None and asker != admin_id:
            protected.append((t.name, f"🆕 {name_of(asker)} hat es neu angefragt"))
            continue
        hits.append(t)
    hits.sort(key=lambda t: -t.size)
    if q.largest:
        hits = hits[: q.largest]
    return hits, protected


def whole(t: Title) -> Item:
    return Item(t.kind, t.arr_id, t.name, t.size, service=t.service)


def _ep_label(season: int, ep: int) -> str:
    return f"S{season:02d}E{ep:02d}"


def plan_parts(t: Title, episodes: list[dict], leaves: list[dict] | None, q: Query,
               admin_id: int | None, now: datetime | None = None) -> list[Item]:
    """Seasons or episodes of one series that match (watched state per episode from Plex)."""
    now = now or datetime.now(timezone.utc)
    viewed: dict[tuple[int, int], int | None] = {}
    for leaf in leaves or []:
        if leaf.get("viewCount"):
            viewed[(int(leaf.get("parentIndex") or 0), int(leaf.get("index") or 0))] = leaf.get("lastViewedAt")

    def ok(e: dict) -> bool:
        key = (int(e.get("seasonNumber") or 0), int(e.get("episodeNumber") or 0))
        if q.watched is True and key not in viewed:
            return False
        if q.watched is False and key in viewed:
            return False
        if q.seen_days:
            last = viewed.get(key)
            if not last or datetime.fromtimestamp(last, timezone.utc) > now - timedelta(days=q.seen_days):
                return False
        if q.older_days:
            added = _dt((e.get("episodeFile") or {}).get("dateAdded"))
            if not added or added > now - timedelta(days=q.older_days):
                return False
        return True

    with_file = [e for e in episodes if e.get("hasFile") and e.get("episodeFileId")]
    good = [e for e in with_file if ok(e)]
    if not good:
        return []

    def item(eps: list[dict], what: str, name: str) -> Item:
        files = list(dict.fromkeys(e["episodeFileId"] for e in eps))
        size = sum((e.get("episodeFile") or {}).get("size") or 0 for e in {e["episodeFileId"]: e for e in eps}.values())
        return Item(what, t.arr_id, name, size, files, [e["id"] for e in eps], "sonarr")

    if q.scope == "episodes":
        eps = sorted(good, key=lambda e: (e.get("seasonNumber") or 0, e.get("episodeNumber") or 0))
        first, last = eps[0], eps[-1]
        span = _ep_label(first.get("seasonNumber") or 0, first.get("episodeNumber") or 0)
        if len(eps) > 1:
            span += "–" + _ep_label(last.get("seasonNumber") or 0, last.get("episodeNumber") or 0)
        out = [item(eps, "episodes", f"{t.title}: {len(eps)} Folge{'n' if len(eps) > 1 else ''} ({span})")]
    else:
        out = []
        for season in sorted({e.get("seasonNumber") or 0 for e in with_file}):
            all_s = [e for e in with_file if (e.get("seasonNumber") or 0) == season]
            if all(e in good for e in all_s):
                label = "Specials" if season == 0 else f"Staffel {season}"
                out.append(item(all_s, "season", f"{t.title} – {label} ({len(all_s)} Folgen)"))
    if q.min_gb:
        out = [i for i in out if i.size >= q.min_gb * 2**30]
    return out


async def delete(radarr: Any, sonarr: Any, item: Item) -> str | None:
    """Delete one item; None = done, else the error. No import exclusion: it can come back any time."""
    try:
        if item.what == "movie":
            await radarr.delete(item.arr_id, delete_files=True)
        elif item.what == "series":
            await sonarr.delete(item.arr_id, delete_files=True)
        else:
            await sonarr.delete_episode_files(item.file_ids)
            await sonarr.set_episodes_monitored(item.episode_ids, False)  # else Sonarr loads them again
    except ArrError as e:
        return f"{item.name}: {e}"
    log.info("Medien-Manager: gelöscht %s (%s)", item.name, size_text(item.size))
    return None


# ---- texts -------------------------------------------------------------------------------------
def scope_question(job: Job) -> tuple[str, list[tuple[str, str]]]:
    names = ", ".join(t.title for t in job.pending_series[:5]) + (" …" if len(job.pending_series) > 5 else "")
    text = (f"🗂 {job.query.text()}\n\nDabei sind {len(job.pending_series)} Serien ({names}). "
            "Was soll bei Serien weg?")
    return text, [("📄 Einzelne passende Folgen", "sc:episodes"), ("📦 Ganze passende Staffeln", "sc:seasons"),
                  ("📺 Nur ganze Serien", "sc:series"), ("✖️ Abbrechen", "no")]


def preview(job: Job) -> tuple[str, list[tuple[str, str]]]:
    items = job.items
    total = sum(i.size for i in items)
    lines = [f"🗂 {job.query.text()}"]
    if not items:
        lines.append("\n👍 Nichts passt – da gibt es nichts zu löschen.")
    else:
        lines.append(f"\n🗑 {len(items)} {'Eintrag' if len(items) == 1 else 'Einträge'} · zusammen {size_text(total)}:")
        lines += [f"{i.icon} {i.name} · {size_text(i.size)}" for i in items[:PREVIEW_LINES]]
        if len(items) > PREVIEW_LINES:
            lines.append(f"… und {len(items) - PREVIEW_LINES} weitere")
    if job.protected:
        lines.append(f"\n🛡 Nicht dabei ({len(job.protected)}):")
        lines += [f"• {n} – {why}" for n, why in job.protected[:8]]
    if not items:
        return "\n".join(lines), []
    lines.append("\nGelöschtes ist nicht gesperrt – du kannst es jederzeit wieder hinzufügen.")
    return "\n".join(lines), [(f"🗑 Alle {len(items)} löschen ({size_text(total)})", "all"),
                              ("👆 Einzeln durchgehen", "one"), ("✖️ Abbrechen", "no")]


def one(job: Job) -> tuple[str, list[tuple[str, str]]]:
    i = job.items[job.pos]
    text = f"🗂 {job.pos + 1}/{len(job.items)}: {i.icon} {i.name} · {size_text(i.size)}"
    if i.what in ("season", "episodes"):
        text += "\n(Die Folgen werden danach nicht mehr überwacht.)"
    return text, [("🗑 Löschen", "del"), ("➡️ Behalten", "skip"), ("🛡 Nie löschen", "keep"), ("⏹ Stopp", "stop")]


def summary(job: Job, cancelled: bool = False) -> str:
    if cancelled and not job.deleted:
        return "✖️ Abgebrochen – nichts gelöscht."
    freed = sum(i.size for i in job.deleted)
    lines = [f"🗑 Gelöscht: {len(job.deleted)} · {size_text(freed)} frei."]
    if job.skipped:
        lines.append(f"➡️ Diesmal behalten: {job.skipped}")
    if job.kept:
        lines.append(f"🛡 Nie löschen: {', '.join(job.kept[:5])}" + (" …" if len(job.kept) > 5 else ""))
    if job.failed:
        lines.append("⚠️ Nicht gelöscht: " + "; ".join(job.failed[:5]))
    if job.deleted:
        lines.append("Gesperrt ist nichts: einfach wieder hinzufügen oder „such nochmal“, wenn du es doch sehen willst.")
    return "\n".join(lines)


def storage_text(report: Report, disks: list[Any], admin_id: int | None, top: int = 10) -> str:
    """Where the space goes and what could go."""
    movies = [t for t in report.titles if t.kind == "movie" and t.files]
    series = [t for t in report.titles if t.kind == "series" and t.files]
    lines = ["💾 <b>Speicher</b>"]
    for d in disks:
        lines.append(f"• {d.path}: {size_text(d.free)} frei von {size_text(d.total)} ({d.used_pct:.0f} % belegt)")
    lines.append(f"🎬 Filme: {len(movies)} · {size_text(sum(t.size for t in movies))}")
    lines.append(f"📺 Serien: {len(series)} · {size_text(sum(t.size for t in series))}")
    big = sorted(movies + series, key=lambda t: -t.size)[:top]
    if big:
        lines.append(f"\n<b>Die {len(big)} größten</b>")
        lines += [f"{'🎬' if t.kind == 'movie' else '📺'} {t.name} · {size_text(t.size)}"
                  + (" ✅ gesehen" if admin_id in t.complete_by else "") for t in big]
    if report.plex_ok and admin_id is not None:
        now = datetime.now(timezone.utc)
        seen = [t for t in movies + series if admin_id in t.complete_by and (t.kind == "movie" or t.ended)]
        old = [t for t in movies + series if admin_id not in t.started_by and t.added
               and t.added < now - timedelta(days=180)]
        lines.append("\n<b>Könnte weg</b>")
        lines.append(f"✅ Von dir gesehen: {len(seen)} · {size_text(sum(t.size for t in seen))}")
        lines.append(f"💤 Über 180 Tage da, von dir nie angefangen: {len(old)} · {size_text(sum(t.size for t in old))}")
        lines.append("<i>Z. B. „Lösche alle Filme, die ich gesehen habe“ – ich zeige erst eine Liste.</i>")
    return "\n".join(lines)
