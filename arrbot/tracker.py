"""Polls Radarr/Sonarr history + queue and notifies chats about tracked items."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from .arr import ArrError, Radarr, Sonarr, _ArrBase
from .state import Store, Tracked, utcnow
from .tools import queue_title

log = logging.getLogger(__name__)

Send = Callable[[int, str], Awaitable[None]]

GRABBED = "grabbed"
IMPORTED = {"downloadFolderImported", "movieFolderImported", "seriesFolderImported"}
FAILED = "downloadFailed"
SEEN_KEEP = 500


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _episode_label(rec: dict) -> str | None:
    ep = rec.get("episode")
    if not ep:
        return None
    return f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"


def _quality(rec: dict) -> str | None:
    return ((rec.get("quality") or {}).get("quality") or {}).get("name")


class Tracker:
    def __init__(
        self,
        radarr: Radarr | None,
        sonarr: Sonarr | None,
        store: Store,
        send: Send,
        no_release_minutes: int,
        track_days: int,
    ):
        self.services: dict[str, _ArrBase] = {}
        if radarr:
            self.services["radarr"] = radarr
        if sonarr:
            self.services["sonarr"] = sonarr
        self.store = store
        self.send = send
        self.no_release = timedelta(minutes=no_release_minutes)
        self.track_for = timedelta(days=track_days)
        self.seen: dict[str, list[int]] = defaultdict(list)

    async def poll(self) -> None:
        for name, svc in self.services.items():
            try:
                await self._poll_history(name, svc)
                await self._poll_queue(name, svc)
            except ArrError as e:
                log.warning("Abfrage von %s fehlgeschlagen: %s", name, e)
        await self._check_no_release()
        self._cleanup()
        self.store.save()

    # ------------------------------------------------------------------
    async def _poll_history(self, name: str, svc: _ArrBase) -> None:
        st = self.store.state
        now = utcnow()
        if name not in st.cursors:
            st.cursors[name] = now.isoformat()  # first start: don't replay old history
            return
        since = _parse(st.cursors[name])
        records = await svc.history_since(since)
        seen = self.seen[name]
        newest = since

        # Group per tracked item + event so a season pack doesn't send 10 messages.
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for rec in sorted(records, key=lambda r: r.get("date", "")):
            if rec.get("id") in seen:
                continue
            seen.append(rec.get("id"))
            if rec.get("date"):
                newest = max(newest, _parse(rec["date"]))
            arr_id = rec.get("movieId") if name == "radarr" else rec.get("seriesId")
            key = f"{name}:{arr_id}"
            if key in st.tracked:
                groups[(key, rec.get("eventType", ""))].append(rec)
        del seen[:-SEEN_KEEP]
        st.cursors[name] = newest.isoformat()

        for (key, event), recs in groups.items():
            t = st.tracked[key]
            msg = self._event_message(t, event, recs)
            if not msg:
                continue
            t.last_activity = now.isoformat()
            if event == GRABBED:
                t.grabbed = True
            elif event in IMPORTED and name == "radarr":
                t.completed = True
            await self._notify(t, msg)

    def _event_message(self, t: Tracked, event: str, recs: list[dict]) -> str | None:
        eps = sorted({e for e in (_episode_label(r) for r in recs) if e})
        ep_txt = f" ({', '.join(eps[:10])}{' …' if len(eps) > 10 else ''})" if eps else ""
        first = recs[0]
        data = first.get("data") or {}
        if event == GRABBED:
            details = ", ".join(x for x in (_quality(first), data.get("indexer")) if x)
            return f"⬇️ Download gestartet: {t.title}{ep_txt}" + (f"\n{details}" if details else "")
        if event in IMPORTED:
            return f"✅ Fertig und importiert: {t.title}{ep_txt}"
        if event == FAILED:
            reason = data.get("message") or "unbekannter Grund"
            return (
                f"❌ Download fehlgeschlagen: {t.title}{ep_txt}\nGrund: {reason}\n"
                "Es wird automatisch nach einer Alternative gesucht (wenn in Radarr/Sonarr aktiviert)."
            )
        return None

    async def _poll_queue(self, name: str, svc: _ArrBase) -> None:
        st = self.store.state
        for q in await svc.queue():
            arr_id = q.get("movieId") if name == "radarr" else q.get("seriesId")
            t = st.tracked.get(f"{name}:{arr_id}")
            if not t:
                continue
            status = q.get("trackedDownloadStatus")
            state = q.get("trackedDownloadState")
            if status not in ("warning", "error") and state not in ("importBlocked", "failedPending"):
                continue
            messages = [m for sm in q.get("statusMessages") or [] for m in sm.get("messages") or []]
            if q.get("errorMessage"):
                messages.insert(0, q["errorMessage"])
            sig = f"{q.get('downloadId')}|{status}|{state}|{messages[:1]}"
            if sig in st.queue_notified:
                continue
            st.queue_notified[sig] = utcnow().isoformat()
            detail = "\n".join(f"• {m}" for m in messages[:3]) or f"Status: {state or status}"
            await self._notify(
                t,
                f"⚠️ Problem beim Download: {queue_title(q)}\n{detail}\n"
                "Ggf. in Radarr/Sonarr unter Aktivität → Warteschlange manuell eingreifen.",
            )

    async def _check_no_release(self) -> None:
        now = utcnow()
        for t in self.store.state.tracked.values():
            if t.grabbed or t.completed or t.no_release_notified:
                continue
            if now - _parse(t.added_at) >= self.no_release:
                t.no_release_notified = True
                tool = "Radarr" if t.service == "radarr" else "Sonarr"
                await self._notify(
                    t,
                    f"🔍 Für {t.title} wurde bisher kein passender Download gefunden. "
                    f"{tool} sucht automatisch weiter und ich melde mich, sobald etwas startet.",
                )

    def _cleanup(self) -> None:
        st = self.store.state
        now = utcnow()
        for key, t in list(st.tracked.items()):
            if (t.service == "radarr" and t.completed) or now - _parse(t.last_activity) > self.track_for:
                del st.tracked[key]
        cutoff = now - timedelta(days=7)
        st.queue_notified = {k: v for k, v in st.queue_notified.items() if _parse(v) > cutoff}

    async def _notify(self, t: Tracked, text: str) -> None:
        for chat_id in t.chat_ids:
            try:
                await self.send(chat_id, text)
            except Exception:  # noqa: BLE001
                log.exception("Nachricht an %s fehlgeschlagen", chat_id)
