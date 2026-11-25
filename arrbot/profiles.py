"""Per-user profile database (data/profiles.db, SQLite): what someone watches and likes.

Learned in code, without AI tokens:
  - every request / new search: title, year, genres (from Radarr/Sonarr) -> genre and decade weights
  - finished downloads, deleted titles, "nichts davon" on suggestions
Told by the user ("merk dir, ich mag keine Horrorfilme") via the `remember` tool:
  - liked / avoided genres, from year, minimum rating, short free notes

Used for: suggestions (genre boost, skip avoided genres and rejected titles, default year/rating)
and one short line of context for the AI (~30 tokens) so answers fit the person.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# TMDB genre ids (movie + tv) -> German name. Radarr/Sonarr deliver English names, mapped below.
TMDB_GENRES = {
    28: "Action", 12: "Abenteuer", 16: "Animation", 35: "Komödie", 80: "Krimi", 99: "Dokumentarfilm",
    18: "Drama", 10751: "Familie", 14: "Fantasy", 36: "Historie", 27: "Horror", 10402: "Musik",
    9648: "Mystery", 10749: "Liebesfilm", 878: "Science Fiction", 10770: "TV-Film", 53: "Thriller",
    10752: "Kriegsfilm", 37: "Western", 10759: "Action", 10762: "Kinder", 10763: "News", 10764: "Reality",
    10765: "Science Fiction", 10766: "Seifenoper", 10767: "Talkshow", 10768: "Kriegsfilm",
}
ARR_GENRES = {
    "action": "Action", "adventure": "Abenteuer", "animation": "Animation", "anime": "Animation",
    "comedy": "Komödie", "crime": "Krimi", "documentary": "Dokumentarfilm", "drama": "Drama",
    "family": "Familie", "fantasy": "Fantasy", "history": "Historie", "horror": "Horror", "music": "Musik",
    "musical": "Musik", "mystery": "Mystery", "romance": "Liebesfilm", "science fiction": "Science Fiction",
    "science-fiction": "Science Fiction", "sci-fi": "Science Fiction", "tv movie": "TV-Film",
    "thriller": "Thriller", "suspense": "Thriller", "war": "Kriegsfilm", "western": "Western",
    "children": "Kinder", "kids": "Kinder", "reality": "Reality", "soap": "Seifenoper", "talk show": "Talkshow",
    "action & adventure": "Action", "sci-fi & fantasy": "Science Fiction", "war & politics": "Kriegsfilm",
}
GENRE_NAMES = sorted(set(TMDB_GENRES.values()))
# Weight change per event.
WEIGHTS = {"requested": 1.0, "finished": 0.5, "watched": 1.5, "deleted": -1.0, "not_interested": -0.3}
MAX_NOTES = 5
DAY_NAMES = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def genre_de(name: str) -> str | None:
    """'Science Fiction' / 'sci-fi' / 'Horror' / 'komödie' -> canonical German genre name."""
    n = name.strip().lower()
    for g in GENRE_NAMES:
        if g.lower() == n:
            return g
    return ARR_GENRES.get(n)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Prefs:
    like: list[str] = field(default_factory=list)  # genres the user said they like
    avoid: list[str] = field(default_factory=list)  # genres the user never wants suggested
    from_year: int | None = None
    min_rating: float | None = None
    notes: list[str] = field(default_factory=list)  # short free text, e.g. "schaut mit Kindern"
    digest_day: int | None = None  # weekly overview on this weekday (0 = Monday), None = off


@dataclass
class Profile:
    user_id: int
    prefs: Prefs
    genres: dict[str, float]  # learned weights
    decades: dict[int, int]
    counts: dict[str, int]  # action -> number
    since: str | None
    rejected: set[tuple[str, int]]  # (kind, tmdb id) shown and turned down
    seen: set[tuple[str, int]]  # (kind, tmdb id) requested / deleted: never suggest again

    def top_genres(self, n: int = 3) -> list[str]:
        return [g for g, w in sorted(self.genres.items(), key=lambda x: -x[1]) if w > 0 and g not in self.prefs.avoid][:n]

    def favourite_decade(self) -> int | None:
        if sum(self.decades.values()) < 3:
            return None
        return max(self.decades, key=self.decades.get)

    def genre_bonus(self, genres: list[str]) -> float:
        """0..1 bonus for suggestions: how well the genres fit what this person likes."""
        if not genres:
            return 0.0
        top = max((w for w in self.genres.values() if w > 0), default=0.0)
        learned = sum(max(self.genres.get(g, 0.0), 0.0) for g in genres) / (top * len(genres)) if top else 0.0
        stated = 0.5 if any(g in self.prefs.like for g in genres) else 0.0
        return min(1.0, learned + stated)

    def is_empty(self) -> bool:
        p = self.prefs
        return not (self.genres or p.like or p.avoid or p.from_year or p.min_rating or p.notes)

    def context_line(self, name: str = "") -> str:
        """Short context for the AI (~30 tokens). Empty if nothing is known yet."""
        if self.is_empty():
            return ""
        p, parts = self.prefs, []
        likes = list(dict.fromkeys(p.like + self.top_genres()))[:4]
        if likes:
            parts.append("mag " + ", ".join(likes))
        if p.avoid:
            parts.append("meidet " + ", ".join(p.avoid))
        if p.from_year:
            parts.append(f"Titel ab {p.from_year}")
        if p.min_rating:
            parts.append(f"ab ⭐{p.min_rating:g}")
        if p.notes:
            parts.append("; ".join(p.notes[:3]))
        return f"[Nutzerprofil{' ' + name if name else ''}: {'; '.join(parts)}]"


class ProfileStore:
    """SQLite, one file. Calls are tiny and synchronous (a few ms), guarded by a lock."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "profiles.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, action TEXT NOT NULL, kind TEXT NOT NULL,
                tmdb INTEGER, tvdb INTEGER, title TEXT, year INTEGER, genres TEXT, at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_user ON events(user_id, action);
            CREATE TABLE IF NOT EXISTS genres (
                user_id INTEGER NOT NULL, genre TEXT NOT NULL, weight REAL NOT NULL,
                PRIMARY KEY (user_id, genre));
            CREATE TABLE IF NOT EXISTS prefs (
                user_id INTEGER PRIMARY KEY, data TEXT NOT NULL, updated TEXT NOT NULL);
            -- "Sag mir, wenn es auf Deutsch kommt": checked once a day
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL, ext_id INTEGER NOT NULL,
                title TEXT NOT NULL, season INTEGER, known INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL, checked TEXT, UNIQUE(user_id, kind, ext_id, season));
        """)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ---- learning ---------------------------------------------------------------------------
    def learn(self, user_id: int, action: str, kind: str, title: str, *, tmdb: int | None = None,
              tvdb: int | None = None, year: int | None = None, genres: list[str] | None = None) -> None:
        """Record what happened. genres: any names (English from Radarr/Sonarr or German)."""
        de = list(dict.fromkeys(g for g in (genre_de(x) for x in genres or []) if g))
        w = WEIGHTS.get(action, 0.0)
        try:
            with self._lock, self._db:
                if action in ("requested", "finished", "watched"):  # same title again: only the newest entry
                    self._db.execute(
                        "DELETE FROM events WHERE user_id=? AND action=? AND kind=? AND tmdb IS ? AND tvdb IS ?",
                        (user_id, action, kind, tmdb, tvdb))
                self._db.execute(
                    "INSERT INTO events(user_id, action, kind, tmdb, tvdb, title, year, genres, at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (user_id, action, kind, tmdb, tvdb, title, year, json.dumps(de, ensure_ascii=False), utcnow()))
                for g in de:
                    self._db.execute(
                        "INSERT INTO genres(user_id, genre, weight) VALUES (?,?,?) "
                        "ON CONFLICT(user_id, genre) DO UPDATE SET weight = weight + excluded.weight",
                        (user_id, g, w))
        except sqlite3.Error as e:  # a profile must never break a download
            log.error("Profil-DB: %s", e)

    def known(self, user_id: int, action: str) -> set[tuple[str, int | None, int | None]]:
        """(kind, tmdb, tvdb) of titles with this action, e.g. everything already learned as watched."""
        with self._lock:
            rows = self._db.execute("SELECT kind, tmdb, tvdb FROM events WHERE user_id=? AND action=?",
                                    (user_id, action)).fetchall()
        return {(r["kind"], r["tmdb"], r["tvdb"]) for r in rows}

    def has_events(self) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM events LIMIT 1").fetchone() is not None

    # ---- stated preferences ------------------------------------------------------------------
    def prefs(self, user_id: int) -> Prefs:
        with self._lock:
            row = self._db.execute("SELECT data FROM prefs WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return Prefs()
        try:
            return Prefs(**json.loads(row["data"]))
        except (TypeError, ValueError):
            return Prefs()

    def save_prefs(self, user_id: int, p: Prefs) -> None:
        data = json.dumps(p.__dict__, ensure_ascii=False)
        with self._lock, self._db:
            self._db.execute("INSERT INTO prefs(user_id, data, updated) VALUES (?,?,?) "
                             "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                             (user_id, data, utcnow()))

    def remember(self, user_id: int, *, like: list[str] | None = None, avoid: list[str] | None = None,
                 from_year: int | None = None, min_rating: float | None = None, note: str | None = None,
                 forget: list[str] | None = None) -> str:
        """Apply what the user said. Returns a German confirmation (shown as is)."""
        p = self.prefs(user_id)
        done: list[str] = []
        unknown: list[str] = []

        def genres(names: list[str] | None) -> list[str]:
            out = []
            for n in names or []:
                g = genre_de(n)
                (out.append(g) if g else unknown.append(n))
            return out

        for g in genres(like):
            p.avoid = [x for x in p.avoid if x != g]
            if g not in p.like:
                p.like.append(g)
                done.append(f"du magst {g}")
        for g in genres(avoid):
            p.like = [x for x in p.like if x != g]
            if g not in p.avoid:
                p.avoid.append(g)
                done.append(f"keine {g}-Vorschläge mehr")
        if from_year:
            p.from_year = int(from_year)
            done.append(f"Vorschläge erst ab {p.from_year}")
        if min_rating:
            p.min_rating = float(min_rating)
            done.append(f"nur ab ⭐ {p.min_rating:g}".replace(".", ","))
        if note and note.strip():
            p.notes = ([n for n in p.notes if n != note.strip()] + [note.strip()[:100]])[-MAX_NOTES:]
            done.append(f"„{note.strip()[:100]}“")
        for f in forget or []:
            f_low = f.strip().lower()
            g = genre_de(f)
            before = (list(p.like), list(p.avoid), list(p.notes), p.from_year, p.min_rating)
            if g:
                p.like = [x for x in p.like if x != g]
                p.avoid = [x for x in p.avoid if x != g]
            if f_low in ("jahr", "year", "from_year", "jahre"):
                p.from_year = None
            if f_low in ("bewertung", "rating", "min_rating", "sterne"):
                p.min_rating = None
            p.notes = [n for n in p.notes if f_low not in n.lower()]
            if before != (p.like, p.avoid, p.notes, p.from_year, p.min_rating):
                done.append(f"„{f}“ vergessen")
        if done:
            self.save_prefs(user_id, p)
        text = ("👍 Gemerkt: " + ", ".join(done) + ".") if done else "🤔 Da war nichts, was ich mir merken könnte."
        if unknown:
            text += f"\nGenre unbekannt: {', '.join(unknown)}. Bekannt sind: {', '.join(GENRE_NAMES)}."
        return text

    def forget_all(self, user_id: int) -> None:
        with self._lock, self._db:
            for table in ("events", "genres", "prefs", "watches"):
                self._db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))  # noqa: S608 - fixed names

    # ---- German watch list --------------------------------------------------------------------
    def add_watch(self, user_id: int, kind: str, ext_id: int, title: str, season: int | None = None) -> bool:
        """kind movie: ext_id = TMDB id; series: TVDB id. False if it was already on the list."""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO watches(user_id, kind, ext_id, title, season, created) VALUES (?,?,?,?,?,?)",
                (user_id, kind, ext_id, title, season, utcnow()))
            return cur.rowcount > 0

    def watches(self, user_id: int | None = None) -> list[dict]:
        with self._lock:
            q, args = "SELECT * FROM watches", ()
            if user_id is not None:
                q, args = q + " WHERE user_id=?", (user_id,)
            return [dict(r) for r in self._db.execute(q + " ORDER BY id", args)]

    def update_watch(self, watch_id: int, known: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE watches SET known=?, checked=? WHERE id=?", (known, utcnow(), watch_id))

    def drop_watch(self, watch_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM watches WHERE id=?", (watch_id,))

    def set_digest(self, user_id: int, weekday: int | None) -> None:
        p = self.prefs(user_id)
        p.digest_day = weekday
        self.save_prefs(user_id, p)

    def digest_users(self, weekday: int) -> list[int]:
        with self._lock:
            rows = self._db.execute("SELECT user_id, data FROM prefs").fetchall()
        return [r["user_id"] for r in rows if json.loads(r["data"]).get("digest_day") == weekday]

    # ---- reading ----------------------------------------------------------------------------
    def profile(self, user_id: int) -> Profile:
        with self._lock:
            genres = {r["genre"]: r["weight"] for r in
                      self._db.execute("SELECT genre, weight FROM genres WHERE user_id=?", (user_id,))}
            rows = self._db.execute("SELECT action, kind, tmdb, year, at FROM events WHERE user_id=?",
                                    (user_id,)).fetchall()
        counts = Counter(r["action"] for r in rows)
        decades = Counter((r["year"] // 10) * 10 for r in rows if r["year"] and r["action"] in ("requested", "finished"))
        rejected = {(r["kind"], r["tmdb"]) for r in rows if r["action"] == "not_interested" and r["tmdb"]}
        seen = {(r["kind"], r["tmdb"]) for r in rows if r["action"] in ("requested", "deleted", "watched") and r["tmdb"]}
        since = min((r["at"] for r in rows), default=None)
        return Profile(user_id, self.prefs(user_id), genres, dict(decades), dict(counts), since, rejected, seen)

    def recent(self, user_id: int, limit: int = 8) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, tmdb, title FROM events WHERE user_id=? AND action IN ('requested','finished') "
                "AND tmdb IS NOT NULL ORDER BY id DESC LIMIT ?", (user_id, limit * 2)).fetchall()
        out, keys = [], set()
        for r in rows:
            if (r["kind"], r["tmdb"]) not in keys:
                keys.add((r["kind"], r["tmdb"]))
                out.append(dict(r))
        return out[:limit]


def render(p: Profile, name: str) -> str:
    """'Was weißt du über mich?' – HTML for Telegram."""
    if p.is_empty() and not p.counts:
        return (f"🧑 <b>Profil {name}</b>\n\nIch weiß noch nichts über dich. Mit jeder Anfrage lerne ich deinen "
                "Geschmack, und du kannst mir sagen, was du magst: „Merk dir, ich mag keine Horrorfilme.“")
    out = [f"🧑 <b>Profil {name}</b>"]
    if p.since:
        out.append(f"<i>seit {datetime.fromisoformat(p.since):%d.%m.%Y}</i>")
    pr = p.prefs
    told = []
    if pr.like:
        told.append(f"👍 Mag: {', '.join(pr.like)}")
    if pr.avoid:
        told.append(f"🚫 Keine Vorschläge: {', '.join(pr.avoid)}")
    if pr.from_year:
        told.append(f"📅 Vorschläge ab {pr.from_year}")
    if pr.min_rating:
        told.append(f"⭐ Mindestens {pr.min_rating:g}".replace(".", ","))
    told += [f"📝 {n}" for n in pr.notes]
    if told:
        out += ["", "<b>Hast du mir gesagt</b>"] + told
    learned = []
    total = sum(w for w in p.genres.values() if w > 0)
    top = [(g, w) for g, w in sorted(p.genres.items(), key=lambda x: -x[1]) if w > 0][:5]
    if top:
        learned.append("🎭 Lieblingsgenres: " + ", ".join(f"{g} {w / total:.0%}".replace(".", ",") for g, w in top))
    dec = p.favourite_decade()
    if dec:
        learned.append(f"📼 Am liebsten aus den {dec % 100:02d}ern" if dec < 2000 else f"📼 Am liebsten ab {dec}")
    c = p.counts
    learned.append(f"📊 {c.get('requested', 0)} angefragt · {c.get('finished', 0)} fertig geladen · "
                   + (f"{c['watched']} in Plex gesehen · " if c.get("watched") else "") +
                   f"{c.get('deleted', 0)} gelöscht · {c.get('not_interested', 0)} Vorschläge abgelehnt")
    if pr.digest_day is not None:
        out.insert(2 if p.since else 1, f"📰 Wochenrückblick jeden {DAY_NAMES[pr.digest_day]}")
    out += ["", "<b>Hab ich gelernt</b>"] + learned
    out += ["", "<i>Ändern einfach per Nachricht, z. B. „Vergiss Horror“ oder „Ich mag Krimis“.</i>"]
    return "\n".join(out)
