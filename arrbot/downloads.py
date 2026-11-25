"""'Meine Downloads': running, waiting and finished downloads as a phone-friendly Telegram message.

Built entirely in code from Radarr/Sonarr queue + SABnzbd + the bot's own history, so
showing it costs no AI tokens.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Callable

from .sabnzbd import SabQueue, eta_text, size_text
from .state import Store, utcnow

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
    kind: str  # movie | series
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
                  tz: tzinfo, now: datetime | None = None) -> Snapshot:
    now = now or utcnow()
    tracked = store.state.tracked
    by_download: dict[str, Item] = {}
    items: list[Item] = []
    for service, svc in (("radarr", radarr), ("sonarr", sonarr)):
        if svc is None:
            continue
        for q in await svc.queue():
            arr_id = q.get("movieId") if service == "radarr" else q.get("seriesId")
            key = f"{service}:{arr_id}"
            did = q.get("downloadId") or f"{key}:{q.get('id')}"
            ep = _episode(q)
            if did in by_download:  # season pack: one download, many episode records
                if ep and ep not in by_download[did].episodes:
                    by_download[did].episodes.append(ep)
                continue
            if service == "radarr":
                m = q.get("movie") or {}
                title = f"{m.get('title') or q.get('title', '?')}" + (f" ({m['year']})" if m.get("year") else "")
            else:
                title = (q.get("series") or {}).get("title") or q.get("title", "?")
            t = tracked.get(key)
            size, left = float(q.get("size") or 0), float(q.get("sizeleft") or 0)
            it = Item(
                key=key, kind="movie" if service == "radarr" else "series", title=title,
                episodes=[ep] if ep else [], state=_arr_state(q),
                pct=100 * (size - left) / size if size else None,
                mb_total=size / 2**20, mb_left=left / 2**20,
                owners=list(t.chat_ids) if t else [],
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
    return "🎬" if it.kind == "movie" else "📺"


def _eps(it: Item) -> str:
    if not it.episodes:
        return ""
    eps = sorted(it.episodes)
    txt = ", ".join(eps[:4]) + (f" +{len(eps) - 4}" if len(eps) > 4 else "")
    return f" · {txt}"


def render(snap: Snapshot, viewer: int, show_all: bool, name_of: Callable[[int], str],
           tz: tzinfo, now: datetime | None = None) -> str:
    """HTML for Telegram. show_all: admin view with everybody's downloads and who asked for them."""
    now = now or utcnow()
    e = lambda s: html.escape(str(s), quote=False)  # noqa: E731

    def mine(owners: list[int]) -> bool:
        return show_all or viewer in owners

    def who(owners: list[int]) -> str:
        if not show_all:
            return ""
        others = [name_of(o) for o in owners if o != viewer]
        return f"\n👤 {e(', '.join(others))}" if others else ("" if owners else "\n👤 <i>außerhalb des Bots</i>")

    items = [i for i in snap.items if mine(i.owners)]
    active = [i for i in items if i.state in (DOWNLOADING, IMPORTING, PROBLEM)]
    waiting = [i for i in items if i.state in (QUEUED, PAUSED)]
    waiting.sort(key=lambda i: i.position if i.position is not None else 999)
    searching = [(t, o) for t, o in snap.searching if mine(o)]
    finished = [f for f in snap.finished if mine(f.get("chat_ids") or [])]
    paused = [(t, o, u) for t, o, u in snap.paused if mine(o)]

    out = [f"<b>📥 {'Alle Downloads' if show_all else 'Deine Downloads'}</b>"]
    if snap.sab_ok is False:
        out.append("<i>⚠️ SABnzbd gerade nicht erreichbar – Angaben aus Radarr/Sonarr.</i>")
    elif snap.sab_paused:
        out.append("⏸ <b>SABnzbd ist pausiert</b>")
    elif snap.sab_speed and any(i.state == DOWNLOADING for i in items):
        out.append(f"⚡ {e(snap.sab_speed)} gesamt")

    if active:
        out.append("\n<b>⬇️ Läuft gerade</b>")
        for it in active[:MAX_ACTIVE]:
            lines = [f"{_icon(it)} <b>{e(it.title)}</b>{e(_eps(it))}"]
            if it.state == IMPORTING:
                lines.append(f"<code>{bar(100)}</code> 100 %")
                lines.append("📦 Wird importiert …")
            else:
                pct = f"{it.pct:.0f} %" if it.pct is not None else "? %"
                lines.append(f"<code>{bar(it.pct)}</code> {pct}")
                if it.mb_total:
                    done = size_text(it.mb_total - it.mb_left)
                    speed = f" · ⚡ {e(it.speed)}" if it.speed else ""
                    lines.append(f"{done} von {size_text(it.mb_total)}{speed}")
                if it.eta:
                    lines.append(f"⏱ {e(it.eta)}")
            if it.state == PROBLEM:
                lines.append("⚠️ " + e(it.problems[0] if it.problems else "Problem beim Download"))
            out.append("\n".join(lines) + who(it.owners))
        if len(active) > MAX_ACTIVE:
            out.append(f"<i>… und {len(active) - MAX_ACTIVE} weitere</i>")

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
        out += [f"• {e(t)}" + who(o).replace("\n", " ") for t, o in searching[:MAX_WAITING]]

    if paused:
        out.append("\n<b>⏸ Pausiert</b>")
        for t, o, until in paused[:MAX_WAITING]:
            when = f"sucht wieder ab {datetime.fromisoformat(until).astimezone(tz):%d.%m.}" if until else "bis du „weiter“ sagst"
            out.append(f"• {e(t)} <i>– {when}</i>" + who(o).replace("\n", " "))

    if finished:
        out.append("\n<b>✅ Fertig</b>")
        for f in finished[:MAX_FINISHED]:
            out.append(f"• {e(f['title'])} <i>– {_when(f['at'], now, tz)}</i>" + who(f.get("chat_ids") or []).replace("\n", " "))

    if not (active or waiting or searching or paused or finished):
        out.append("\nGerade läuft nichts und es ist noch nichts fertig geworden. Schreib mir einfach, was du sehen willst. 🎬")
    out.append(f"\n<i>Stand {now.astimezone(tz):%H:%M} Uhr</i>")
    return "\n".join(out)[:4096]
