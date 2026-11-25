"""Admin: what users ask, what worked and where there were problems (data/profiles.db, table activity).

Recorded in code for every message (no AI tokens): the question, which bot functions ran, the outcome
and the tokens used. Button actions (added, deleted, ...) are recorded too. Kept for 90 days.
"""

from __future__ import annotations

import html
import json
import logging
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

KEEP_DAYS = 90
OK, PROBLEM, NOTHING, ERROR, TALK, ACTION, LIMIT = "ok", "problem", "nothing", "error", "talk", "action", "limit"
ICONS = {OK: "✅", PROBLEM: "⚠️", NOTHING: "🤷", ERROR: "❌", TALK: "💬", ACTION: "👆", LIMIT: "⛔"}
LABELS = {OK: "geklappt", PROBLEM: "Problem", NOTHING: "nichts gefunden", ERROR: "Fehler", TALK: "nur Antwort",
          ACTION: "per Button", LIMIT: "Limit erreicht"}

TOOL_DE = {
    "search_movies": "Filmsuche", "search_series": "Seriensuche", "add_movie": "Film hinzufügen",
    "add_series": "Serie hinzufügen", "offer_titles": "Auswahl", "search_existing": "neu suchen",
    "download_status": "Download-Status", "suggest": "Vorschläge", "top_titles": "Top-Listen",
    "german_info": "Deutsch-Info", "german_fix": "Deutsch prüfen/ersetzen", "clean_episodes": "Folgen löschen",
    "remove_title": "Löschen", "pause_title": "Pausieren", "resume_title": "Fortsetzen", "open_url": "Link",
    "remember": "Vorliebe merken", "my_profile": "Profil", "calendar": "Kalender", "disk_space": "Speicherplatz",
    "notify_german": "Deutsch-Benachrichtigung", "weekly_digest": "Wochenrückblick", "plex_status": "Plex",
    "list_quality_profiles": "Qualitätsprofile", "admin_stats": "Statistik", "admin_users": "Nutzer",
    "admin_check": "API-Check", "admin_set_limit": "Limit setzen", "admin_user": "Nutzer verwalten",
    "admin_media": "Mediathek", "admin_quality_profile": "Profil anlegen", "admin_activity": "Aktivität",
}
NOTHING_HINTS = ("🤷", "nicht gefunden", "keine treffer", "nichts gefunden", "kein passend")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Entry:
    id: int
    user_id: int
    at: datetime
    text: str
    tools: list[str]
    outcome: str
    detail: str
    tokens: int


def classify(tools: list[str], errors: list[str], reply: str, failed: str | None) -> tuple[str, str]:
    """(outcome, detail) of one message."""
    if failed:
        return ERROR, failed
    if errors:
        return PROBLEM, errors[-1]
    low = (reply or "").lower()
    if reply.startswith("⚠️"):
        return ERROR, reply[2:].strip()
    if any(h in low for h in NOTHING_HINTS):
        return NOTHING, reply.strip().split("\n")[0]
    if not tools:
        return TALK, ""
    return OK, ""


class ActivityStore:
    def __init__(self, data_dir: str):
        path = Path(data_dir) / "profiles.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, at TEXT NOT NULL, text TEXT,
                tools TEXT, outcome TEXT NOT NULL, detail TEXT, tokens INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS activity_at ON activity(at);
        """)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def record(self, user_id: int, text: str, tools: list[str], outcome: str, detail: str = "",
               tokens: int = 0, at: datetime | None = None) -> None:
        try:
            with self._lock, self._db:
                self._db.execute(
                    "INSERT INTO activity(user_id, at, text, tools, outcome, detail, tokens) VALUES (?,?,?,?,?,?,?)",
                    (user_id, (at or utcnow()).isoformat(timespec="seconds"), (text or "")[:300],
                     json.dumps(list(dict.fromkeys(tools))), outcome, (detail or "")[:300], tokens))
                cutoff = (utcnow() - timedelta(days=KEEP_DAYS)).isoformat()
                self._db.execute("DELETE FROM activity WHERE at < ?", (cutoff,))
        except sqlite3.Error as e:  # the log must never break answering
            log.error("Aktivität nicht gespeichert: %s", e)

    def entries(self, days: int = 7, user_id: int | None = None) -> list[Entry]:
        since = (utcnow() - timedelta(days=days)).isoformat()
        q, args = "SELECT * FROM activity WHERE at >= ?", [since]
        if user_id is not None:
            q, args = q + " AND user_id = ?", args + [user_id]
        with self._lock:
            rows = self._db.execute(q + " ORDER BY id DESC", args).fetchall()
        return [Entry(r["id"], r["user_id"], datetime.fromisoformat(r["at"]), r["text"] or "",
                      json.loads(r["tools"] or "[]"), r["outcome"], r["detail"] or "", r["tokens"] or 0) for r in rows]

    def forget(self, user_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM activity WHERE user_id=?", (user_id,))


# ---- view ---------------------------------------------------------------------------------------
def _line(x: Entry, tz: tzinfo, name_of: Callable[[int], str] | None, detail: bool) -> str:
    e = lambda s: html.escape(str(s), quote=False)  # noqa: E731
    who = f"<b>{e(name_of(x.user_id))}</b> · " if name_of else ""
    what = f"„{e(x.text[:80])}“" if x.text else ""
    tools = ", ".join(TOOL_DE.get(t, t) for t in x.tools[:3])
    line = f"{ICONS.get(x.outcome, '•')} {who}{x.at.astimezone(tz):%d.%m. %H:%M} {what}"
    if tools:
        line += f" <i>→ {e(tools)}</i>"
    if detail and x.detail:
        line += f"\n      {e(x.detail[:140])}"
    return line


def render(entries: list[Entry], days: int, tz: tzinfo, name_of: Callable[[int], str],
           mode: str = "all", user_name: str | None = None) -> str:
    """mode: all | problems | users"""
    title = f"📋 <b>Aktivität{' von ' + html.escape(user_name) if user_name else ''}</b> – {days} Tage"
    if not entries:
        return title + "\n\nNoch nichts passiert."
    asked = [x for x in entries if x.outcome != ACTION]
    count = Counter(x.outcome for x in entries)
    out = [title, f"{len(asked)} Anfragen · " + " · ".join(
        f"{ICONS[k]} {count[k]} {LABELS[k]}" for k in (OK, ACTION, NOTHING, PROBLEM, ERROR, TALK, LIMIT) if count[k])]
    if asked:
        rate = sum(1 for x in asked if x.outcome in (OK, TALK)) / len(asked)
        out.append(f"Erfolgsquote: {rate:.0%}".replace("%", " %"))
    used = Counter(t for x in entries for t in x.tools)
    if used:
        out.append("Am meisten genutzt: " + ", ".join(f"{TOOL_DE.get(t, t)} {n}" for t, n in used.most_common(5)))

    if mode == "users":
        out.append("\n<b>👥 Pro Nutzer</b>")
        by_user: dict[int, list[Entry]] = {}
        for x in entries:
            by_user.setdefault(x.user_id, []).append(x)
        for uid, xs in sorted(by_user.items(), key=lambda kv: -len(kv[1])):
            c = Counter(x.outcome for x in xs)
            bad = c[PROBLEM] + c[ERROR] + c[NOTHING]
            last = max(x.at for x in xs).astimezone(tz)
            top = Counter(t for x in xs for t in x.tools).most_common(2)
            out.append(f"• <b>{html.escape(name_of(uid))}</b>: {len(xs)} · ✅ {c[OK] + c[ACTION]}"
                       + (f" · ⚠️ {bad}" if bad else "") + f" · zuletzt {last:%d.%m. %H:%M}"
                       + (f"\n   meist: {', '.join(TOOL_DE.get(t, t) for t, _ in top)}" if top else ""))
        return "\n".join(out)[:4096]

    problems = [x for x in entries if x.outcome in (PROBLEM, ERROR, NOTHING)]
    if problems:
        out.append(f"\n<b>⚠️ Probleme ({len(problems)})</b>")
        out += [_line(x, tz, name_of, True) for x in problems[:12]]
        tips = _tips(problems)
        if tips:
            out.append("\n<b>💡 Auffällig</b>")
            out += tips
    if mode == "all":
        out.append("\n<b>🕘 Zuletzt</b>")
        out += [_line(x, tz, name_of, False) for x in entries[:12]]
    elif not problems:
        out.append("\nKeine Probleme. 🎉")
    return "\n".join(out)[:4096]


def _tips(problems: list[Entry]) -> list[str]:
    """Patterns worth a look, e.g. the same error again and again."""
    out = []
    details = Counter(p.detail.split(":")[0][:60] for p in problems if p.detail)
    for d, n in details.most_common(2):
        if n >= 3:
            out.append(f"• {n}× „{html.escape(d)}“")
    nothing = [p for p in problems if p.outcome == NOTHING]
    if len(nothing) >= 3:
        out.append(f"• {len(nothing)}× nichts gefunden – Titel falsch geschrieben oder noch nicht erschienen?")
    return out
