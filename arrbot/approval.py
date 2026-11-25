"""Admin: approve wishes first ("Wünsche von Anna erst freigeben", "… wenn es Horrorfilme sind",
"… wenn der Download größer als 20 GB ist").

A rule names a user (or everybody), optionally a kind (movie/series), genres and a minimum download size.
Without size the wish is held before anything is added. With size (movies) the movie is added without a
download, the best release is looked up, and only a bigger one is held (unmonitored, so Radarr does not
grab it on its own). The admin gets "Freigeben / Ablehnen" buttons; the user hears the result.
Admins never need approval. Rules and open wishes live in data/profiles.db (survive restarts).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .profiles import genre_de

KIND = {"movie": "Filme", "series": "Serien", "all": "Wünsche"}


@dataclass
class Rule:
    id: int
    user_id: int | None  # None = everybody
    kind: str = "all"  # movie | series | all
    genres: list[str] = field(default_factory=list)  # German names, any of them
    min_gb: float | None = None  # only downloads bigger than this (movies)

    def text(self, name_of: Callable[[int], str]) -> str:
        who = name_of(self.user_id) if self.user_id is not None else "allen"
        out = f"{KIND[self.kind]} von {who}"
        if self.genres:
            out += f", wenn Genre {' oder '.join(self.genres)}"
        if self.min_gb:
            out += f", wenn der Download größer als {self.min_gb:g} GB ist" + (" (nur Filme)" if self.kind != "movie" else "")
        return out

    def matches(self, user_id: int, kind: str, genres: list[str]) -> bool:
        if self.user_id is not None and self.user_id != user_id:
            return False
        if self.kind != "all" and self.kind != kind:
            return False
        if self.min_gb and kind != "movie":
            return False
        if self.genres:
            have = {(genre_de(g) or g).lower() for g in genres}
            if not have & {g.lower() for g in self.genres}:
                return False
        return True


@dataclass
class Pending:
    id: str
    user_id: int
    kind: str
    ext_id: int
    title: str
    reason: str
    monitor: str | None = None
    seasons: list[int] | None = None
    profile: str | None = None
    arr_id: int | None = None  # size check: the movie is in Radarr already (unmonitored)
    release: dict | None = None  # size check: the release to grab on approval
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


class ApprovalStore:
    def __init__(self, data_dir: str):
        path = Path(data_dir) / "profiles.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS approval_rules (
                id INTEGER PRIMARY KEY, user_id INTEGER, kind TEXT, genres TEXT, min_gb REAL);
            CREATE TABLE IF NOT EXISTS approval_pending (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # rules
    def add(self, user_id: int | None, kind: str = "all", genres: list[str] | None = None,
            min_gb: float | None = None) -> Rule:
        if kind not in KIND:
            raise ValueError(kind)
        names = [genre_de(g) or g.strip().title() for g in genres or [] if g.strip()]
        with self._lock, self._db:
            cur = self._db.execute("INSERT INTO approval_rules(user_id, kind, genres, min_gb) VALUES (?,?,?,?)",
                                   (user_id, kind, json.dumps(names), min_gb or None))
        return Rule(cur.lastrowid, user_id, kind, names, min_gb or None)

    def rules(self) -> list[Rule]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM approval_rules ORDER BY id").fetchall()
        return [Rule(r["id"], r["user_id"], r["kind"], json.loads(r["genres"] or "[]"), r["min_gb"]) for r in rows]

    def remove(self, rule_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM approval_rules WHERE id=?", (rule_id,)).rowcount > 0

    def forget(self, user_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM approval_rules WHERE user_id=?", (user_id,))

    def applies(self, user_id: int, kind: str) -> list[Rule]:
        """Rules that could hold a wish of this user/kind (genres and size still to check)."""
        return [r for r in self.rules() if (r.user_id is None or r.user_id == user_id)
                and r.kind in ("all", kind) and not (r.min_gb and kind != "movie")]

    # open wishes
    def hold(self, p: Pending) -> Pending:
        p.id = p.id or uuid.uuid4().hex[:10]
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO approval_pending VALUES (?, ?)", (p.id, json.dumps(asdict(p))))
        return p

    def take(self, pid: str) -> Pending | None:
        with self._lock, self._db:
            row = self._db.execute("SELECT data FROM approval_pending WHERE id=?", (pid,)).fetchone()
            if not row:
                return None
            self._db.execute("DELETE FROM approval_pending WHERE id=?", (pid,))
        return Pending(**json.loads(row["data"]))

    def pending(self) -> list[Pending]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM approval_pending").fetchall()
        return sorted((Pending(**json.loads(r["data"])) for r in rows), key=lambda p: p.created)


def render_rules(rules: list[Rule], pending: list[Pending], name_of: Callable[[int], str]) -> str:
    out = ["✋ Freigaben"]
    if rules:
        out += [f"{r.id}. {r.text(name_of)}" for r in rules]
    else:
        out.append("Keine Regeln – alle Wünsche gehen direkt durch.\nZ. B.: „Wünsche von Anna erst freigeben“, "
                   "„… wenn es Horrorfilme sind“, „… wenn der Download größer als 20 GB ist“.")
    if pending:
        out.append(f"\n⏳ Warten auf dich ({len(pending)}):")
        out += [f"• {name_of(p.user_id)}: {p.title} – {p.reason}" for p in pending[:15]]
    return "\n".join(out)
