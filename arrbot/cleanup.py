"""Admin: cleanup rules ("Filme, die alle gesehen haben, nach 30 Tagen löschen").

Rules live in data/profiles.db. Once a day the bot applies them to the media check:
  - watched:   everyone with a linked Plex has seen it completely, the last view is `days` ago
  - unwatched: on the server for more than `days` days and nobody has started it
Optional: only when free space is below `low_space_gb`. By default the admin gets a list with a
"delete all" button; only rules with auto=True delete by themselves. Titles marked "keep" are never
touched.
"""

from __future__ import annotations

import html
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .media import Report, Title, size_text

WHAT = {"watched": "von allen gesehen", "unwatched": "nie angeschaut"}
KIND = {"movie": "Filme", "series": "Serien", "all": "Filme und Serien"}


@dataclass
class Rule:
    id: int
    what: str  # watched | unwatched
    kind: str  # movie | series | all
    days: int
    low_space_gb: int | None = None
    auto: bool = False

    @property
    def text(self) -> str:
        when = "seit dem letzten Anschauen" if self.what == "watched" else "auf dem Server"
        out = f"{KIND[self.kind]}, {WHAT[self.what]}, nach {self.days} Tagen {when}"
        if self.low_space_gb:
            out += f", nur wenn unter {self.low_space_gb} GB frei"
        return out + (" → löschen automatisch" if self.auto else " → ich frage dich")


class RuleStore:
    def __init__(self, data_dir: str):
        path = Path(data_dir) / "profiles.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS cleanup_rules (
                id INTEGER PRIMARY KEY, what TEXT NOT NULL, kind TEXT NOT NULL, days INTEGER NOT NULL,
                low_space_gb INTEGER, auto INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS cleanup_keep (service TEXT, arr_id INTEGER, title TEXT,
                PRIMARY KEY (service, arr_id));
            CREATE TABLE IF NOT EXISTS cleanup_settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def add(self, what: str, kind: str, days: int, low_space_gb: int | None = None, auto: bool = False) -> Rule:
        if what not in WHAT or kind not in KIND or days < 1:
            raise ValueError("Regel unvollständig")
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO cleanup_rules(what, kind, days, low_space_gb, auto) VALUES (?,?,?,?,?)",
                (what, kind, int(days), low_space_gb or None, int(bool(auto))))
        return Rule(cur.lastrowid, what, kind, int(days), low_space_gb or None, bool(auto))

    def rules(self) -> list[Rule]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM cleanup_rules ORDER BY id").fetchall()
        return [Rule(r["id"], r["what"], r["kind"], r["days"], r["low_space_gb"], bool(r["auto"])) for r in rows]

    def remove(self, rule_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM cleanup_rules WHERE id=?", (rule_id,)).rowcount > 0

    def keep(self, service: str, arr_id: int, title: str = "") -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO cleanup_keep VALUES (?,?,?)", (service, arr_id, title))

    def unkeep(self, service: str, arr_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM cleanup_keep WHERE service=? AND arr_id=?",
                                    (service, arr_id)).rowcount > 0

    def kept_titles(self) -> list[tuple[str, int, str]]:
        with self._lock:
            return [(r["service"], r["arr_id"], r["title"] or "?")
                    for r in self._db.execute("SELECT * FROM cleanup_keep ORDER BY title")]

    def setting(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM cleanup_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        with self._lock, self._db:
            if value is None:
                self._db.execute("DELETE FROM cleanup_settings WHERE key=?", (key,))
            else:
                self._db.execute("INSERT OR REPLACE INTO cleanup_settings VALUES (?,?)", (key, value))

    def kept(self) -> set[tuple[str, int]]:
        with self._lock:
            return {(r["service"], r["arr_id"]) for r in self._db.execute("SELECT * FROM cleanup_keep")}


def matches(rule: Rule, report: Report, kept: set[tuple[str, int]], now: datetime | None = None) -> list[Title]:
    now = now or datetime.now(timezone.utc)
    if rule.what == "watched":
        items = [t for t in report.section("watched")
                 if t.last_viewed is None or t.last_viewed < now - timedelta(days=rule.days)]
    else:
        items = report.section("unwatched", rule.days)
    return [t for t in items if (rule.kind == "all" or t.kind == rule.kind) and (t.service, t.arr_id) not in kept]


def render_rules(rules: list[Rule]) -> str:
    if not rules:
        return ("🧹 Noch keine Aufräumregeln.\nZ. B.: „Lösch Filme, die alle gesehen haben, nach 30 Tagen“ oder "
                "„Serien, die keiner nach einem Jahr angeschaut hat, nur wenn der Platz knapp wird“.")
    return "🧹 <b>Aufräumregeln</b>\n" + "\n".join(f"{r.id}. {html.escape(r.text)}" for r in rules) + \
        "\n\n<i>Ich prüfe einmal am Tag (11 Uhr). Titel mit „🛡 Behalten“ fasse ich nie an.</i>"


def render_hits(rule: Rule, hits: list[Title], deleted: bool = False) -> tuple[str, list[tuple[str, str]]]:
    size = sum(t.size for t in hits)
    head = f"🧹 Regel {rule.id}: {rule.text}\n"
    head += (f"🗑 Gelöscht: {len(hits)} Titel, {size_text(size)} frei" if deleted
             else f"{len(hits)} Titel passen · zusammen {size_text(size)}")
    lines = [head] + [f"{'🎬' if t.kind == 'movie' else '📺'} {t.name} · {size_text(t.size)}" for t in hits[:15]]
    if len(hits) > 15:
        lines.append(f"… und {len(hits) - 15} weitere")
    if deleted:
        return "\n".join(lines), []
    buttons = [(f"🗑 Alle {len(hits)} löschen ({size_text(size)})", f"c:all:{rule.id}")]
    buttons += [(f"🛡 Behalten: {t.name[:30]}", f"c:keep:{t.service}:{t.arr_id}") for t in hits[:6]]
    return "\n".join(lines), buttons
