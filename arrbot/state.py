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

    @property
    def key(self) -> str:
        return f"{self.service}:{self.arr_id}"


@dataclass
class State:
    cursors: dict[str, str] = field(default_factory=dict)
    tracked: dict[str, Tracked] = field(default_factory=dict)
    # downloadId|status -> iso time; avoids repeating the same queue warning.
    queue_notified: dict[str, str] = field(default_factory=dict)


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
            )
        except (ValueError, TypeError) as e:
            log.error("state.json unlesbar (%s), starte leer", e)
            return State()

    def save(self) -> None:
        data = {
            "cursors": self.state.cursors,
            "tracked": {k: asdict(v) for k, v in self.state.tracked.items()},
            "queue_notified": self.state.queue_notified,
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
