"""Small JSON-file store for tracked downloads and poll cursors."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

FINISHED_KEEP = 200
REQUESTS_KEEP = 1000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Tracked:
    service: str  # "radarr" | "sonarr"
    arr_id: int
    title: str
    chat_ids: list[int]
    added_at: str
    last_activity: str
    grabbed: bool = False
    completed: bool = False
    no_release_notified: bool = False
    # [downloadId, polls waited] of grabbed downloads whose SABnzbd finish time is not announced yet
    pending_downloads: list = field(default_factory=list)
    # Series: a running "job" (many episodes) is announced once and summed up when done:
    # {"started": iso, "last": iso, "grabbed": [labels], "imported": [labels], "failed": n}
    job: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.service}:{self.arr_id}"


@dataclass
class State:
    cursors: dict[str, str] = field(default_factory=dict)
    tracked: dict[str, Tracked] = field(default_factory=dict)
    # downloadId|status -> iso time; avoids repeating the same queue warning.
    queue_notified: dict[str, str] = field(default_factory=dict)
    # Finished imports for "Meine Downloads": {"title", "service", "chat_ids", "at"} (newest last)
    finished: list[dict] = field(default_factory=list)
    # What each chat asked for (for suggestions): {"kind", "tmdb", "tvdb", "title", "chat_id", "at"}
    requests: list[dict] = field(default_factory=list)
    # Paused searches by key "radarr:7": {"title", "chat_ids", "until" (iso or None = until resumed)}
    paused: dict[str, dict] = field(default_factory=dict)
    # Muted chats "chat_id" -> {"until": iso, "missed": n, "lines": [first lines of missed messages]}
    muted: dict[str, dict] = field(default_factory=dict)


class Store:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()
        self.writable = True
        self.save()  # surfaces permission problems at startup instead of on the first add

    def _load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return State(
                cursors=raw.get("cursors", {}),
                tracked={k: Tracked(**v) for k, v in raw.get("tracked", {}).items()},
                queue_notified=raw.get("queue_notified", {}),
                finished=raw.get("finished", []),
                requests=raw.get("requests", []),
                paused=raw.get("paused", {}),
                muted=raw.get("muted", {}),
            )
        except (ValueError, TypeError) as e:
            log.error("state.json unlesbar (%s), starte leer", e)
            return State()

    def save(self) -> None:
        data = {
            "cursors": self.state.cursors,
            "tracked": {k: asdict(v) for k, v in self.state.tracked.items()},
            "queue_notified": self.state.queue_notified,
            "finished": self.state.finished,
            "requests": self.state.requests,
            "paused": self.state.paused,
            "muted": self.state.muted,
        }
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            self.writable = True
        except OSError as e:
            # Keep working from memory; only a restart would lose tracked items.
            if self.writable:
                log.error(
                    "Kann %s nicht schreiben (%s). Beobachtete Downloads gehen beim Neustart verloren. "
                    "PUID/PGID in docker-compose.yml prüfen.", self.path, e,
                )
            self.writable = False

    def track(self, service: str, arr_id: int, title: str, chat_id: int) -> Tracked:
        now = utcnow().isoformat()
        key = f"{service}:{arr_id}"
        t = self.state.tracked.get(key)
        if t:
            if chat_id not in t.chat_ids:
                t.chat_ids.append(chat_id)
            t.last_activity = now
            t.completed = False
            t.grabbed = False
            t.no_release_notified = False
            t.added_at = now
        else:
            t = Tracked(service, arr_id, title, [chat_id], now, now)
            self.state.tracked[key] = t
        self.save()
        return t

    on_finish = None  # (Tracked, title) -> None, set by the bot (user profiles)

    def finish(self, t: Tracked, title: str) -> None:
        if self.on_finish:
            try:
                self.on_finish(t, title)
            except Exception:  # noqa: BLE001 - a profile must never break the tracker
                log.exception("Profil bei 'fertig' nicht aktualisiert")
        self.state.finished.append(
            {"title": title, "service": t.service, "chat_ids": list(t.chat_ids), "at": utcnow().isoformat()}
        )
        del self.state.finished[:-FINISHED_KEEP]

    def owners(self, kind: str, ext_id: int | None, key: str) -> set[int]:
        """Chats that asked for a title (requests history plus tracking)."""
        field_name = "tmdb" if kind == "movie" else "tvdb"
        found = {r["chat_id"] for r in self.state.requests if r["kind"] == kind and ext_id and r.get(field_name) == ext_id}
        t = self.state.tracked.get(key)
        return found | set(t.chat_ids if t else [])

    def forget(self, key: str) -> None:
        self.state.tracked.pop(key, None)
        self.state.paused.pop(key, None)
        self.save()

    def record_request(self, chat_id: int, kind: str, title: str, tmdb: int | None, tvdb: int | None = None) -> None:
        reqs = [r for r in self.state.requests
                if not (r["chat_id"] == chat_id and r["kind"] == kind and r.get("tmdb") == tmdb and r.get("tvdb") == tvdb)]
        reqs.append({"kind": kind, "tmdb": tmdb, "tvdb": tvdb, "title": title, "chat_id": chat_id,
                     "at": utcnow().isoformat()})
        self.state.requests = reqs[-REQUESTS_KEEP:]
        self.save()
