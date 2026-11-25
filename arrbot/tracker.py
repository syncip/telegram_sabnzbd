"""Polls Radarr/Sonarr history + queue and notifies chats about tracked items."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, tzinfo
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .arr import ArrError, Radarr, Sonarr, _ArrBase
from .sabnzbd import SABnzbd, SabError, SabQueue, eta_text, size_text
from .state import Store, Tracked, utcnow
from .tools import queue_title, resume_search

log = logging.getLogger(__name__)

# send(chat_id, text) or send(chat_id, text, buttons=[(label, callback_data), ...])
Send = Callable[..., Awaitable[None]]

GRABBED = "grabbed"
IMPORTED = {"downloadFolderImported", "movieFolderImported", "seriesFolderImported"}
FAILED = "downloadFailed"
SEEN_KEEP = 500
PENDING_POLLS = 10  # how long to wait for a grabbed download to show up in SABnzbd


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
        sab: SABnzbd | None = None,
        tz: tzinfo | None = None,
        hint: Callable[[Tracked], Awaitable[str | None]] | None = None,
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
        self.sab = sab
        self.hint = hint  # extra text for the "no release" notice, e.g. German release dates
        self.tz = tz or ZoneInfo("Europe/Berlin")
        self.sab_queue: SabQueue | None = None
        self._sab_failed = False

    async def poll(self) -> None:
        await self._poll_sab()
        for name, svc in self.services.items():
            try:
                await self._poll_history(name, svc)
                await self._poll_queue(name, svc)
            except ArrError as e:
                log.warning("Abfrage von %s fehlgeschlagen: %s", name, e)
        await self._check_pending()
        await self._check_paused()
        await self._check_no_release()
        self._cleanup()
        self.store.save()

    # ------------------------------------------------------------------
    async def _poll_sab(self) -> None:
        if not self.sab:
            return
        try:
            self.sab_queue = await self.sab.queue()
            if self._sab_failed:
                log.info("SABnzbd wieder erreichbar")
            self._sab_failed = False
        except SabError as e:
            self.sab_queue = None
            if not self._sab_failed:  # log once, not every minute
                log.warning("%s", e)
            self._sab_failed = True

    def sab_line(self, download_id: str | None) -> str | None:
        """'⏱ fertig ca. 21:45 Uhr (noch 23 min) · 4,2 GB' if SABnzbd knows the download."""
        q = self.sab_queue
        slot = q.slots.get(download_id or "") if q else None
        if not slot:
            return None
        return f"⏱ {eta_text(slot, utcnow(), self.tz, q.paused)} · {size_text(slot.mb_total)}"

    async def _check_pending(self) -> None:
        if not self.sab_queue:
            return
        for t in self.store.state.tracked.values():
            still = []
            for download_id, waited in t.pending_downloads:
                line = self.sab_line(download_id)
                if line:
                    await self._notify(t, f"{t.title}\n{line}")
                elif waited + 1 < PENDING_POLLS:
                    still.append([download_id, waited + 1])
            t.pending_downloads = still

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
                if self.sab:
                    eta = []
                    for did in dict.fromkeys(r.get("downloadId") for r in recs if r.get("downloadId")):
                        line = self.sab_line(did)
                        if line:
                            eta.append(line)
                        else:  # SABnzbd hasn't picked it up yet; announce the time on a later poll
                            t.pending_downloads.append([did, 0])
                    if eta:
                        msg += "\n" + eta[0]
            elif event in IMPORTED:
                t.completed = name == "radarr"
                eps = self._episodes(recs)
                self.store.finish(t, t.title + (f" ({eps})" if eps else ""))
            await self._notify(t, msg)

    @staticmethod
    def _episodes(recs: list[dict]) -> str:
        eps = sorted({e for e in (_episode_label(r) for r in recs) if e})
        return f"{', '.join(eps[:10])}{' …' if len(eps) > 10 else ''}" if eps else ""

    def _event_message(self, t: Tracked, event: str, recs: list[dict]) -> str | None:
        eps = self._episodes(recs)
        ep_txt = f" ({eps})" if eps else ""
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

    async def _check_paused(self) -> None:
        """Paused with a date ('in 3 Tagen nochmal'): monitor again, search, tell the chats."""
        st = self.store.state
        now = utcnow()
        for key, entry in list(st.paused.items()):
            if not entry.get("until") or _parse(entry["until"]) > now:
                continue
            service, raw_id = key.split(":")
            svc = self.services.get(service)
            if not svc:
                continue
            try:
                await resume_search(svc, int(raw_id))
            except ArrError as e:
                log.warning("Pause von %s nicht beendet: %s", entry.get("title"), e)
                continue
            del st.paused[key]
            for chat in entry.get("chat_ids") or []:
                self.store.track(service, int(raw_id), entry.get("title", "?"), chat)
            t = st.tracked.get(key)
            if t:
                await self._notify(t, f"▶️ Die Pause ist vorbei: Ich suche jetzt wieder nach {entry.get('title')}.")

    async def _check_no_release(self) -> None:
        if self.no_release <= timedelta(0):
            return  # NO_RELEASE_NOTICE_MINUTES = 0: never send this notice
        now = utcnow()
        for t in self.store.state.tracked.values():
            if t.grabbed or t.completed or t.no_release_notified or t.key in self.store.state.paused:
                continue
            if now - _parse(t.added_at) >= self.no_release:
                t.no_release_notified = True
                tool = "Radarr" if t.service == "radarr" else "Sonarr"
                msg = (
                    f"🔍 Für {t.title} wurde bisher kein passender Download gefunden. "
                    f"{tool} sucht automatisch weiter und ich melde mich, sobald etwas startet."
                )
                if self.hint:
                    try:
                        extra = await self.hint(t)
                    except Exception:  # noqa: BLE001 - the notice itself matters more
                        log.exception("Hinweis für %s fehlgeschlagen", t.title)
                        extra = None
                    if extra:
                        msg += "\n" + extra
                # One tap instead of typing: pause for a few days, delete, or just acknowledge.
                await self._notify(t, msg, [
                    ("⏸ In 3 Tagen nochmal", f"n:p3:{t.key}"),
                    ("⏸ In 7 Tagen nochmal", f"n:p7:{t.key}"),
                    ("🗑 Löschen", f"n:rm:{t.key}"),
                    ("🔔 Bei Deutsch melden", f"n:w:{t.key}"),
                    ("👍 Okay", "n:ok"),
                ])

    def _cleanup(self) -> None:
        st = self.store.state
        now = utcnow()
        for key, t in list(st.tracked.items()):
            if (t.service == "radarr" and t.completed) or now - _parse(t.last_activity) > self.track_for:
                del st.tracked[key]
        cutoff = now - timedelta(days=7)
        st.queue_notified = {k: v for k, v in st.queue_notified.items() if _parse(v) > cutoff}
        keep = now - self.track_for
        st.finished = [f for f in st.finished if _parse(f["at"]) > keep]

    is_admin = staticmethod(lambda _uid: True)  # set by the bot: users get no pause/delete buttons
    USER_BUTTONS = ("n:w:", "n:ok")

    async def _notify(self, t: Tracked, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        for chat_id in t.chat_ids:
            mine = buttons if not buttons or self.is_admin(chat_id) else                 [b for b in buttons if b[1].startswith(self.USER_BUTTONS)]
            try:
                if mine:
                    await self.send(chat_id, text, mine)
                else:
                    await self.send(chat_id, text)
            except Exception:  # noqa: BLE001
                log.exception("Nachricht an %s fehlgeschlagen", chat_id)
