"""Polls Radarr/Sonarr history + queue and notifies chats about tracked items."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, tzinfo
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from . import notify
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


def _span(labels: list[str], limit: int = 6) -> str:
    """'S01E01–S01E13, S02E01': long lists shortened to a range per season."""
    labels = sorted(set(labels))
    if len(labels) <= limit:
        return ", ".join(labels)
    seasons: dict[str, list[str]] = defaultdict(list)
    for x in labels:
        seasons[x[:3]].append(x)
    return ", ".join(f"{v[0]}–{v[-1]}" if len(v) > 1 else v[0] for v in seasons.values())


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
        self.in_queue: dict[str, set] = {}  # service -> ids (movieId/seriesId) with downloads in the queue

    async def poll(self) -> None:
        await self._poll_sab()
        for name, svc in self.services.items():
            try:
                await self._poll_history(name, svc)
                await self._poll_queue(name, svc)
            except ArrError as e:
                log.warning("Abfrage von %s fehlgeschlagen: %s", name, e)
        await self._check_pending()
        await self._check_jobs()
        await self._check_paused()
        await self._check_no_release()
        await self._check_mutes()
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
                    await self._notify(t, f"{t.title}\n{line}", kind="start")
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
            if name == "sonarr" and self._job_event(t, event, recs, now):
                continue  # part of a running job: summed up when it is done
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
            await self._notify(t, msg, kind="start" if event == GRABBED else "problem" if event == FAILED else "done")

    def _job_event(self, t: Tracked, event: str, recs: list[dict], now: datetime) -> bool:
        """Series: the first grab starts a job (announced as usual), later grabs and imports only count.
        True = no message now."""
        labels = [x for x in (_episode_label(r) for r in recs) if x]
        job = t.job
        if event == GRABBED:
            if not job:
                t.job = {"started": now.isoformat(), "last": now.isoformat(), "grabbed": labels,
                         "imported": [], "failed": 0}
                return False  # the start is announced
            job["grabbed"] = list(dict.fromkeys(job["grabbed"] + labels))
            job["last"] = now.isoformat()
            t.grabbed = True
            return True
        if not job:
            return False
        job["last"] = now.isoformat()
        if event in IMPORTED:
            job["imported"] = list(dict.fromkeys(job["imported"] + labels))
            self.store.finish(t, t.title + (f" ({self._episodes(recs)})" if labels else ""))
            return True
        if event == FAILED:
            job["failed"] = job.get("failed", 0) + max(len(labels), 1)
            return len(job["grabbed"]) > 1  # a single download: say it right away
        return False

    JOB_QUIET = timedelta(minutes=3)  # no news for this long and nothing in the queue: the job is done

    async def _check_jobs(self) -> None:
        now = utcnow()
        queued = self.in_queue.get("sonarr")
        if queued is None:
            return  # queue unknown (Sonarr not reachable): don't guess
        for t in self.store.state.tracked.values():
            job = t.job
            if not job or t.service != "sonarr" or t.arr_id in queued or now - _parse(job["last"]) < self.JOB_QUIET:
                continue
            got, want, failed = job.get("imported", []), job.get("grabbed", []), job.get("failed", 0)
            t.job = {}
            if len(want) <= 1 and not failed and len(got) <= 1:
                if got:  # one episode: the usual message
                    await self._notify(t, f"✅ Fertig und importiert: {t.title} ({got[0]})", kind="done")
                continue
            n = len(want)
            lines = [f"✅ {t.title}: {len(got)} von {n} Folge{'n' if n != 1 else ''} fertig importiert"
                     + (f" ({_span(got)})" if got else "") + "."]
            if failed:
                lines.append(f"❌ {failed} Download{'s' if failed != 1 else ''} fehlgeschlagen – Sonarr sucht Ersatz.")
            missing = [x for x in want if x not in got]
            if missing and len(got) + failed < n:
                lines.append(f"⏳ Nicht angekommen: {_span(missing)}. Details: /downloads")
            await self._notify(t, "\n".join(lines), kind="done")

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
            more = ("\nWeitere Folgen melde ich nicht einzeln – es gibt eine Zusammenfassung, wenn alles fertig ist."
                    if t.service == "sonarr" else "")
            return f"⬇️ Download gestartet: {t.title}{ep_txt}" + (f"\n{details}" if details else "") + more
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
        records = await svc.queue()
        self.in_queue[name] = {q.get("movieId") if name == "radarr" else q.get("seriesId") for q in records}
        for q in records:
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
                kind="problem",
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
                await self._notify(t, msg, self.no_release_buttons(t.key), kind="nothing")

    @staticmethod
    def no_release_buttons(key: str) -> list[tuple[str, str]]:
        return [
            ("⏸ In 3 Tagen nochmal", f"n:p3:{key}"),
            ("⏸ In 7 Tagen nochmal", f"n:p7:{key}"),
            ("🗑 Löschen", f"n:rm:{key}"),
            ("🔔 Bei Deutsch melden", f"n:w:{key}"),
            ("👍 Okay", "n:ok"),
        ]

    async def tell(self, chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None,
                   kind: str = "info") -> None:
        """One chat; users only get the buttons meant for them."""
        if not notify.wants(self.store, chat_id, kind) or self.muted(chat_id, text):
            return
        if buttons and not self.is_admin(chat_id):
            buttons = [b for b in buttons if b[1].startswith(self.USER_BUTTONS)]
        try:
            if buttons:
                await self.send(chat_id, text, buttons)
            else:
                await self.send(chat_id, text)
        except Exception:  # noqa: BLE001
            log.exception("Nachricht an %s fehlgeschlagen", chat_id)

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

    def muted(self, chat_id: int, text: str) -> bool:
        """Muted chat: count the message for the summary afterwards instead of sending it."""
        m = self.store.state.muted.get(str(chat_id))
        if not m or _parse(m["until"]) <= utcnow():
            return False
        m["missed"] = m.get("missed", 0) + 1
        first = text.strip().split("\n", 1)[0][:120]
        m["lines"] = (m.get("lines") or [])[-19:] + [first]
        return True

    async def _check_mutes(self) -> None:
        now = utcnow()
        for chat, m in list(self.store.state.muted.items()):
            if _parse(m["until"]) > now:
                continue
            del self.store.state.muted[chat]
            n = m.get("missed", 0)
            if not n:
                text = "🔔 Die Stummschaltung ist vorbei – in der Zeit gab es keine Meldungen."
            else:
                shown = m.get("lines") or []
                text = (f"🔔 Die Stummschaltung ist vorbei. In der Zeit gab es {n} Meldung{'en' if n != 1 else ''}:\n"
                        + "\n".join(f"• {x}" for x in shown[-8:]) + ("\n…" if n > 8 else "")
                        + "\nAlles Aktuelle: /downloads")
            try:
                await self.send(int(chat), text)
            except Exception:  # noqa: BLE001
                log.exception("Nachricht an %s fehlgeschlagen", chat)

    async def _notify(self, t: Tracked, text: str, buttons: list[tuple[str, str]] | None = None,
                      kind: str = "info") -> None:
        """kind: start | done | problem | nothing (each chat can switch them off) | info (always)."""
        for chat_id in t.chat_ids:
            if not notify.wants(self.store, chat_id, kind) or self.muted(chat_id, text):
                continue
            mine = buttons if not buttons or self.is_admin(chat_id) else                 [b for b in buttons if b[1].startswith(self.USER_BUTTONS)]
            try:
                if mine:
                    await self.send(chat_id, text, mine)
                else:
                    await self.send(chat_id, text)
            except Exception:  # noqa: BLE001
                log.exception("Nachricht an %s fehlgeschlagen", chat_id)
