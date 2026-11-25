"""🚨 Spam check and overuse warnings (all in code, no AI tokens). Admins are never checked.

Spam -> the user is blocked right away, as a precaution; the admins get a message with an unblock button:
  - more than BURST_MSGS messages within BURST_SECONDS
  - the same message REPEAT_MSGS times within REPEAT_SECONDS
  - more than ADDS_BLOCK titles added within one hour
Overuse -> a warning to the user and the admins, at most once per day (or month):
  - ADDS_WARN titles added within one hour
  - 80 % of a request/cost limit used, or the limit reached
  - far more requests today than the others (UNUSUAL_FACTOR x the median, at least UNUSUAL_MIN)
Every block and warning is logged (table guard_events) for the monthly report.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BURST_MSGS, BURST_SECONDS = 8, 60
REPEAT_MSGS, REPEAT_SECONDS = 4, 600
ADDS_WARN, ADDS_BLOCK = 10, 25
WARN_SHARE = 0.8
UNUSUAL_FACTOR, UNUSUAL_MIN = 3, 15
KEEP_DAYS = 400
BLOCK, WARN, UNBLOCK = "block", "warn", "unblock"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Verdict:
    kind: str  # block | warn
    reason: str  # German, for user and admin
    key: str = ""  # what triggered it (dedupe)


@dataclass
class Event:
    at: datetime
    user_id: int
    kind: str
    detail: str


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


class Guard:
    def __init__(self, data_dir: str, enabled: bool = True):
        self.enabled = enabled
        path = Path(data_dir) / "profiles.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS guard_events (at TEXT, user_id INTEGER, kind TEXT, detail TEXT)")
            cutoff = (utcnow() - timedelta(days=KEEP_DAYS)).isoformat()
            self._db.execute("DELETE FROM guard_events WHERE at < ?", (cutoff,))
        self.msgs: dict[int, deque] = defaultdict(deque)
        self.texts: dict[int, deque] = defaultdict(deque)
        self.adds: dict[int, deque] = defaultdict(deque)
        self.warned: set[tuple[int, str, str]] = set()  # (user, key, period): warn once

    # ---- log --------------------------------------------------------------------------------
    def log(self, uid: int, kind: str, detail: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO guard_events VALUES (?, ?, ?, ?)", (utcnow().isoformat(), uid, kind, detail))

    def events(self, since: datetime, until: datetime | None = None) -> list[Event]:
        until = until or utcnow() + timedelta(days=1)
        with self._lock:
            rows = self._db.execute("SELECT at, user_id, kind, detail FROM guard_events WHERE at >= ? AND at < ? "
                                    "ORDER BY at", (since.isoformat(), until.isoformat())).fetchall()
        return [Event(datetime.fromisoformat(a), u, k, d) for a, u, k, d in rows]

    def forget(self, uid: int) -> None:
        self.msgs.pop(uid, None)
        self.texts.pop(uid, None)
        self.adds.pop(uid, None)

    def _once(self, uid: int, key: str, period: str) -> bool:
        if (uid, key, period) in self.warned:
            return False
        self.warned.add((uid, key, period))
        return True

    @staticmethod
    def _trim(q: deque, older: datetime) -> None:
        while q and (q[0][0] if isinstance(q[0], tuple) else q[0]) < older:
            q.popleft()

    # ---- checks -----------------------------------------------------------------------------
    def message(self, uid: int, text: str, now: datetime | None = None) -> Verdict | None:
        if not self.enabled:
            return None
        now = now or utcnow()
        q = self.msgs[uid]
        q.append(now)
        self._trim(q, now - timedelta(seconds=BURST_SECONDS))
        if len(q) > BURST_MSGS:
            return Verdict(BLOCK, f"{len(q)} Nachrichten in {BURST_SECONDS} Sekunden", "burst")
        t = self.texts[uid]
        t.append((now, _norm(text)))
        self._trim(t, now - timedelta(seconds=REPEAT_SECONDS))
        same = sum(1 for _, x in t if x == _norm(text))
        if _norm(text) and same >= REPEAT_MSGS:
            return Verdict(BLOCK, f"{same}× dieselbe Nachricht in {REPEAT_SECONDS // 60} Minuten", "repeat")
        return None

    def added(self, uid: int, now: datetime | None = None) -> Verdict | None:
        if not self.enabled:
            return None
        now = now or utcnow()
        q = self.adds[uid]
        q.append(now)
        self._trim(q, now - timedelta(hours=1))
        if len(q) > ADDS_BLOCK:
            return Verdict(BLOCK, f"{len(q)} Titel in einer Stunde hinzugefügt", "adds")
        if len(q) >= ADDS_WARN and self._once(uid, "adds", now.strftime("%Y-%m-%d")):
            return Verdict(WARN, f"{len(q)} Titel in einer Stunde hinzugefügt", "adds")
        return None

    def limits(self, uid: int, usage: Any, limits: dict, now: datetime | None = None) -> Verdict | None:
        """usage: users.Usage, limits: users.limits_for(uid). Warns at 80 % and when reached."""
        if not self.enabled:
            return None
        now = now or utcnow()
        names = {"tag": "Anfragen heute", "monat": "Anfragen diesen Monat", "kosten": "Kosten diesen Monat",
                 "tokens": "Tokens diesen Monat"}
        for key, limit in limits.items():
            if key not in names or not limit:
                continue
            used = usage.value(key)
            period = now.strftime("%Y-%m-%d" if key == "tag" else "%Y-%m")
            if used >= limit and self._once(uid, f"{key}:full", period):
                return Verdict(WARN, f"Limit erreicht: {names[key]} {_amount(key, used)} von {_amount(key, limit)}",
                               f"{key}:full")
            if used >= WARN_SHARE * limit and self._once(uid, f"{key}:80", period):
                return Verdict(WARN, f"{names[key]}: {_amount(key, used)} von {_amount(key, limit)} verbraucht",
                               f"{key}:80")
        return None

    def unusual(self, day_requests: dict[int, int], now: datetime | None = None) -> list[tuple[int, Verdict]]:
        """Users with far more requests today than the median of all active users (once per day)."""
        if not self.enabled:
            return []
        now = now or utcnow()
        active = [n for n in day_requests.values() if n > 0]
        if len(active) < 2:
            return []
        median = statistics.median(active)
        out = []
        for uid, n in day_requests.items():
            if n >= UNUSUAL_MIN and n >= UNUSUAL_FACTOR * max(median, 1) \
                    and self._once(uid, "unusual", now.strftime("%Y-%m-%d")):
                out.append((uid, Verdict(WARN, f"{n} Anfragen heute – üblich sind etwa {median:g}", "unusual")))
        return out


def _amount(key: str, v: float) -> str:
    if key == "kosten":
        return f"{v:.2f} $".replace(".", ",")
    return f"{int(v):,}".replace(",", ".")
