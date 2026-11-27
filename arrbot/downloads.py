"""'Meine Downloads': running, waiting and finished downloads as a phone-friendly Telegram message.

Built entirely in code from Radarr/Sonarr queue + SABnzbd + the bot's own history, so
showing it costs no AI tokens.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Callable

from .arr import ArrError
from .sabnzbd import SabQueue, eta_text, size_text
from .state import Store, utcnow

log = logging.getLogger(__name__)

BAR_WIDTH = 10
MAX_ACTIVE = 8
MAX_WAITING = 8
MAX_FINISHED = 8

DOWNLOADING, QUEUED, PAUSED, IMPORTING, PROBLEM = "downloading", "queued", "paused", "importing", "problem"


def bar(pct: float | None, width: int = BAR_WIDTH) -> str:
    """'█████░░░░░' – monospace so it lines up on every phone."""
    if pct is None:
        return "░" * width
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


@dataclass
class Item:
    key: str  # "radarr:12" – matches Tracked.key
    kind: str  # movie | series | book
    title: str
    episodes: list[str] = field(default_factory=list)
    state: str = QUEUED
    pct: float | None = None
    mb_total: float = 0.0
    mb_left: float = 0.0
    eta: str | None = None
    speed: str | None = None
    position: int | None = None
    problems: list[str] = field(default_factory=list)
    owners: list[int] = field(default_factory=list)


@dataclass
class Snapshot:
    items: list[Item]
    searching: list[tuple[str, list[int]]]  # (title, owners): added, nothing grabbed yet
    finished: list[dict]
    paused: list[tuple[str, list[int], str | None]] = field(default_factory=list)  # (title, owners, until)
    sab_paused: bool = False
    sab_speed: str | None = None
    sab_ok: bool | None = None  # None = SABnzbd not configured


def _episode(q: dict) -> str | None:
    ep = q.get("episode") or {}
    if not ep:
        return None
    return f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"


def _arr_state(q: dict) -> str:
    tracked = q.get("trackedDownloadState") or ""
    status = (q.get("status") or "").lower()
    if q.get("trackedDownloadStatus") in ("warning", "error") or tracked in ("importBlocked", "failedPending", "failed"):
        return PROBLEM
    if tracked in ("importPending", "importing") or status == "completed":
        return IMPORTING
    if status == "paused":
        return PAUSED
    if status == "downloading":
        return DOWNLOADING
    return QUEUED


async def collect(radarr, sonarr, store: Store, sab_queue: SabQueue | None, sab_configured: bool,
                  tz: tzinfo, now: datetime | None = None, chaptarr=None,
                  book_owners: dict[int, list[int]] | None = None) -> Snapshot:
    now = now or utcnow()
    tracked = store.state.tracked
    by_download: dict[str, Item] = {}
    items: list[Item] = []
    for service, svc in (("radarr", radarr), ("sonarr", sonarr), ("chaptarr", chaptarr)):
        if svc is None:
            continue
        try:
            records = await svc.queue()
        except ArrError as e:
            if service != "chaptarr":
                raise
            log.warning("%s", e)  # books are optional: the movie/series overview still comes
            continue
        for q in records:
            arr_id = {"radarr": q.get("movieId"), "sonarr": q.get("seriesId")}.get(service, q.get("bookId"))
            key = f"{service}:{arr_id}"
            did = q.get("downloadId") or f"{key}:{q.get('id')}"
            ep = _episode(q)
            if did in by_download:  # season pack: one download, many episode records
                if ep and ep not in by_download[did].episodes:
                    by_download[did].episodes.append(ep)
                continue
            if service == "chaptarr":
                b, a = q.get("book") or {}, (q.get("author") or {}).get("authorName")
                title = (b.get("title") or q.get("title", "?")) + (f" – {a}" if a else "")
            elif service == "radarr":
                m = q.get("movie") or {}
                title = f"{m.get('title') or q.get('title', '?')}" + (f" ({m['year']})" if m.get("year") else "")
            else:
                title = (q.get("series") or {}).get("title") or q.get("title", "?")
            t = tracked.get(key)
            size, left = float(q.get("size") or 0), float(q.get("sizeleft") or 0)
            it = Item(
                key=key, kind={"radarr": "movie", "sonarr": "series"}.get(service, "book"), title=title,
                episodes=[ep] if ep else [], state=_arr_state(q),
                pct=100 * (size - left) / size if size else None,
                mb_total=size / 2**20, mb_left=left / 2**20,
                owners=list(t.chat_ids) if t else list((book_owners or {}).get(arr_id) or []),
            )
            it.problems = [m for sm in q.get("statusMessages") or [] for m in sm.get("messages") or []]
            if q.get("errorMessage"):
                it.problems.insert(0, q["errorMessage"])
            slot = sab_queue.slots.get(q.get("downloadId") or "") if sab_queue else None
            if slot and it.state != PROBLEM:
                # SABnzbd knows better than Radarr/Sonarr: live percentage, real state, finish time.
                it.pct, it.mb_total, it.mb_left, it.position = slot.percent, slot.mb_total, slot.mb_left, slot.position
                if sab_queue.paused or slot.status == "Paused":
                    it.state = PAUSED
                elif slot.status == "Downloading":
                    it.state = DOWNLOADING
                    it.speed = sab_queue.speed_text
                else:
                    it.state = QUEUED
                it.eta = eta_text(slot, now, tz, sab_queue.paused)
            elif it.state == DOWNLOADING and q.get("timeleft"):
                it.eta = f"noch {q['timeleft']}"
            by_download[did] = it
            items.append(it)

    queued_keys = {i.key for i in items}
    searching = [
        (t.title, list(t.chat_ids))
        for t in tracked.values()
        if not t.grabbed and not t.completed and t.key not in queued_keys and t.key not in store.state.paused
    ]
    paused = [(p.get("title", "?"), list(p.get("chat_ids") or []), p.get("until")) for p in store.state.paused.values()]
    return Snapshot(
        items=items,
        searching=searching,
        paused=paused,
        finished=list(reversed(store.state.finished)),
        sab_paused=bool(sab_queue and sab_queue.paused),
        sab_speed=sab_queue.speed_text if sab_queue else None,
        sab_ok=(sab_queue is not None) if sab_configured else None,
    )


def _when(iso: str, now: datetime, tz: tzinfo) -> str:
    t = datetime.fromisoformat(iso).astimezone(tz)
    days = (now.astimezone(tz).date() - t.date()).days
    if days == 0:
        return f"heute {t:%H:%M}"
    if days == 1:
        return f"gestern {t:%H:%M}"
    return f"{t:%d.%m.}"


def _icon(it: Item) -> str:
    return {"movie": "🎬", "book": "📚"}.get(it.kind, "📺")


def _eps(it: Item) -> str:
    if not it.episodes:
        return ""
    eps = sorted(it.episodes)
    txt = ", ".join(eps[:4]) + (f" +{len(eps) - 4}" if len(eps) > 4 else "")
    return f" · {txt}"




# Common Radarr/Sonarr import messages in plain German (the original text follows in italics).
PROBLEM_TEXT = [
    ("matched to series by id", "Release passt nur per ID, nicht per Name"),
    ("matched to movie by id", "Release passt nur per ID, nicht per Name"),
    ("automatic import is not possible", "Automatischer Import nicht möglich"),
    ("no files found are eligible for import", "Keine importierbaren Dateien gefunden"),
    ("not an upgrade", "Keine Verbesserung zur vorhandenen Datei"),
    ("sample", "Nur ein Sample heruntergeladen"),
    ("unknown series", "Serie nicht erkannt"),
    ("unknown movie", "Film nicht erkannt"),
    ("unable to parse", "Dateiname nicht erkannt"),
    ("episode was not found", "Folge nicht gefunden"),
    ("download client", "Problem mit dem Download-Programm"),
    ("password", "Archiv ist passwortgeschützt"),
]


def problem_text(msg: str) -> str:
    low = msg.lower()
    for needle, text in PROBLEM_TEXT:
        if needle in low:
            return text
    return "Problem beim Download"


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-zäöüß]+", " ", s.lower()).strip()


def title_matches(query: str | None, title: str) -> bool:
    """Every word of the query appears in the title ('married children' ~ 'Married... with Children (1987)')."""
    if not query:
        return True
    t = _norm(title)
    return all(w in t for w in _norm(query).split())


def render(snap: Snapshot, viewer: int, show_all: bool, name_of: Callable[[int], str],
           tz: tzinfo, now: datetime | None = None, show_finished: bool = True, query: str | None = None) -> str:
    """HTML for Telegram. show_all: everybody's downloads (admin). show_finished: include the done list.
    query: only titles matching it (status of one search)."""
    now = now or utcnow()
    e = lambda s: html.escape(str(s), quote=False)  # noqa: E731

    def mine(owners: list[int], title: str = "") -> bool:
        return (show_all or viewer in owners) and title_matches(query, title)

    def who(owners: list[int], inline: bool = False) -> str:
        if not show_all:
            return ""
        others = [name_of(o) for o in owners if o != viewer]
        if not others:
            return ""
        return (" " if inline else "\n") + f"👤 {e(', '.join(others))}"

    items = [i for i in snap.items if mine(i.owners, i.title)]
    running = [i for i in items if i.state in (DOWNLOADING, IMPORTING)]
    problems = [i for i in items if i.state == PROBLEM]
    waiting = sorted((i for i in items if i.state in (QUEUED, PAUSED)),
                     key=lambda i: i.position if i.position is not None else 999)
    searching = [(t, o) for t, o in snap.searching if mine(o, t)]
    finished = [f for f in snap.finished if mine(f.get("chat_ids") or [], f.get("title") or "")]
    paused = [(t, o, u) for t, o, u in snap.paused if mine(o, t)]

    if query:
        out = [f"<b>📥 Status: {e(query)}</b>"]
    else:
        out = [f"<b>📥 {'Alle Downloads' if show_all else 'Deine Downloads'}</b>"]
    if snap.sab_ok is False:
        out.append("<i>⚠️ SABnzbd gerade nicht erreichbar – Angaben aus Radarr/Sonarr.</i>")
    elif snap.sab_paused:
        out.append("⏸ <b>SABnzbd ist pausiert</b>")
    elif snap.sab_speed and any(i.state == DOWNLOADING for i in items):
        out.append(f"⚡ {e(snap.sab_speed)} gesamt")

    if running:
        out.append("\n<b>⬇️ Läuft gerade</b>")
        for it in running[:MAX_ACTIVE]:
            lines = [f"{_icon(it)} <b>{e(it.title)}</b>{e(_eps(it))}"]
            if it.state == IMPORTING:
                lines.append("📦 Fertig geladen, wird importiert …")
            else:
                pct = f"{it.pct:.0f} %" if it.pct is not None else "? %"
                lines.append(f"<code>{bar(it.pct)}</code> {pct}")
                if it.mb_total:
                    speed = f" · ⚡ {e(it.speed)}" if it.speed else ""
                    lines.append(f"{size_text(it.mb_total - it.mb_left)} von {size_text(it.mb_total)}{speed}")
                if it.eta:
                    lines.append(f"⏱ {e(it.eta)}")
            out.append("\n".join(lines) + who(it.owners))
        if len(running) > MAX_ACTIVE:
            out.append(f"<i>… und {len(running) - MAX_ACTIVE} weitere</i>")

    if problems:
        # One entry per title and problem, not one per episode.
        groups: dict[tuple[str, str], list[Item]] = {}
        for it in problems:
            raw = it.problems[0] if it.problems else ""
            groups.setdefault((it.title, problem_text(raw)), []).append(it)
        out.append("\n<b>⚠️ Braucht Eingriff</b>")
        for (title, text), its in list(groups.items())[:MAX_ACTIVE]:
            eps = sorted({ep for i in its for ep in i.episodes})
            ep_txt = (" · " + ", ".join(eps[:4]) + (f" +{len(eps) - 4}" if len(eps) > 4 else "")) if eps else ""
            tool = {"movie": "Radarr", "book": "Chaptarr"}.get(its[0].kind, "Sonarr")
            raw = its[0].problems[0] if its[0].problems else ""
            detail = f"\n   <i>{e(raw[:110] + ('…' if len(raw) > 110 else ''))}</i>" if raw and text == "Problem beim Download" else ""
            owners = sorted({o for i in its for o in i.owners})
            out.append(
                f"{_icon(its[0])} <b>{e(title)}</b>{e(ep_txt)}\n"
                f"   {e(text)}{detail}\n"
                f"   👉 {tool} → Aktivität → Warteschlange → manuell importieren" + who(owners)
            )

    if waiting:
        out.append("\n<b>⏳ Warteschlange</b>")
        for it in waiting[:MAX_WAITING]:
            state = "⏸ pausiert" if it.state == PAUSED else (f"Platz {it.position + 1}" if it.position is not None else "wartet")
            size = f" · {size_text(it.mb_total)}" if it.mb_total else ""
            out.append(f"{_icon(it)} {e(it.title)}{e(_eps(it))}\n    {state}{size}" + who(it.owners))
        if len(waiting) > MAX_WAITING:
            out.append(f"<i>… und {len(waiting) - MAX_WAITING} weitere</i>")

    if searching:
        out.append("\n<b>🔍 Gesucht, noch kein Release</b>")
        out += [f"• {e(t)}" + who(o, inline=True) for t, o in searching[:MAX_WAITING]]
        if len(searching) > MAX_WAITING:
            out.append(f"<i>… und {len(searching) - MAX_WAITING} weitere</i>")

    if paused:
        out.append("\n<b>⏸ Pausiert</b>")
        for t, o, until in paused[:MAX_WAITING]:
            when = f"sucht wieder ab {datetime.fromisoformat(until).astimezone(tz):%d.%m.}" if until else "bis du „weiter“ sagst"
            out.append(f"• {e(t)} <i>– {when}</i>" + who(o, inline=True))

    if show_finished and finished:
        out.append("\n<b>✅ Fertig</b>")
        for f in finished[:MAX_FINISHED]:
            out.append(f"• {e(f['title'])} <i>– {_when(f['at'], now, tz)}</i>" + who(f.get("chat_ids") or [], inline=True))

    if query and not (running or problems or waiting or searching or paused or finished):
        out.append(f"\nZu „{e(query)}“ läuft gerade nichts – weder Download noch Suche."
                   "\nSchon fertig oder noch nicht hinzugefügt? Frag mich einfach. 🎬")
    elif not (running or problems or waiting or searching or paused):
        if show_finished and finished:
            pass
        elif finished:
            out.append("\nGerade läuft nichts. ✅ Fertige Downloads zeigt der Button unten.")
        else:
            out.append("\nGerade läuft nichts. Schreib mir einfach, was du sehen willst. 🎬")
    out.append(f"\n<i>Stand {now.astimezone(tz):%H:%M} Uhr</i>")
    return "\n".join(out)[:4096]
