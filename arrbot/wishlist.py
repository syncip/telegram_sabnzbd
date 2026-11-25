"""📌 Wish list for titles that are not out yet ("Merk dir Avatar 4").

Instead of putting an announced movie into Radarr right away (where it sits for years), the bot keeps
the wish here. Once a day it asks Radarr/Sonarr (their lookup knows the dates, no TMDB key needed):
as soon as there is a date (cinema, digital, disc / first air date), the title is added the normal
way (approval rules apply) and the user hears about it. No AI involved.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Wish:
    id: int
    user_id: int
    kind: str  # movie | series
    ext_id: int  # tmdb (movie) / tvdb (series)
    title: str
    created: str


class WishStore:
    def __init__(self, data_dir: str):
        path = Path(data_dir) / "profiles.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS wishes (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL, ext_id INTEGER NOT NULL,
                title TEXT NOT NULL, created TEXT NOT NULL, UNIQUE(user_id, kind, ext_id));
        """)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def add(self, user_id: int, kind: str, ext_id: int, title: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("INSERT OR IGNORE INTO wishes(user_id, kind, ext_id, title, created) VALUES (?,?,?,?,?)",
                                   (user_id, kind, ext_id, title, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            return cur.rowcount > 0

    def wishes(self, user_id: int | None = None) -> list[Wish]:
        q, args = "SELECT * FROM wishes", ()
        if user_id is not None:
            q, args = q + " WHERE user_id=?", (user_id,)
        with self._lock:
            return [Wish(r["id"], r["user_id"], r["kind"], r["ext_id"], r["title"], r["created"])
                    for r in self._db.execute(q + " ORDER BY id", args)]

    def remove(self, wish_id: int, user_id: int | None = None) -> bool:
        q, args = "DELETE FROM wishes WHERE id=?", [wish_id]
        if user_id is not None:
            q, args = q + " AND user_id=?", args + [user_id]
        with self._lock, self._db:
            return self._db.execute(q, args).rowcount > 0

    def forget(self, user_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM wishes WHERE user_id=?", (user_id,))


def _d(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(raw[:10]) if raw else None
    except ValueError:
        return None


def release_date(kind: str, item: dict) -> tuple[date | None, str]:
    """(first known date, what it is) from a Radarr/Sonarr lookup."""
    if kind == "movie":
        dates = [(d, what) for d, what in ((_d(item.get("inCinemas")), "Kino"), (_d(item.get("digitalRelease")), "digital"),
                                            (_d(item.get("physicalRelease")), "Disc")) if d]
        if not dates:
            return None, ""
        return min(dates)
    first = _d(item.get("firstAired"))
    return (first, "Start") if first else (None, "")


def render(wishes: list[Wish]) -> str:
    if not wishes:
        return ("📌 Deine Wunschliste ist leer.\nZ. B.: „Merk dir Avatar 4“ – ich lege es an, sobald es einen "
                "Termin gibt.")
    out = ["📌 Deine Wunschliste – ich lege die Titel an, sobald es einen Termin gibt:"]
    out += [f"{w.id}. {'🎬' if w.kind == 'movie' else '📺'} {w.title}" for w in wishes]
    return "\n".join(out)


async def check(tools: Any, store: WishStore, today: date | None = None) -> list[tuple[int, str]]:
    """Daily: wishes that got a date are added. Returns (user, message) pairs."""
    from .arr import ArrError
    from .tools import Candidate, Flow, Held

    today = today or date.today()
    out: list[tuple[int, str]] = []
    for w in store.wishes():
        svc = tools.radarr if w.kind == "movie" else tools.sonarr
        if not svc:
            continue
        try:
            item = await (svc.lookup_tmdb(w.ext_id) if w.kind == "movie" else svc.lookup_tvdb(w.ext_id))
        except ArrError as e:
            log.info("Wunschliste %s: %s", w.title, e)
            continue
        if item.get("id"):
            store.remove(w.id)
            out.append((w.user_id, f"📌 {w.title} ist inzwischen schon da – von der Wunschliste genommen."))
            continue
        when, what = release_date(w.kind, item)
        if not when:
            continue
        flow = Flow(f"w{w.id}", w.user_id, Candidate(w.kind, w.ext_id, w.title, item.get("year")), monitor="all")
        date_text = f"{what} {when:%d.%m.%Y}" if when > today else f"{what} seit {when:%d.%m.%Y}"
        try:
            await tools._add(flow)
            msg = f"📅 {w.title} hat jetzt einen Termin ({date_text}). Ich habe es hinzugefügt und melde mich zum Download."
        except Held:
            msg = f"📅 {w.title} hat jetzt einen Termin ({date_text}). Der Wunsch wartet auf die Freigabe durch den Admin."
        except ArrError as e:
            log.warning("Wunschliste %s: %s", w.title, e)
            continue
        store.remove(w.id)
        out.append((w.user_id, msg))
    return out
