"""Plex: link an account right in Telegram (PIN on plex.tv/link, no password), what was watched, progress.

Each user links their own Plex account, so "gesehen" and "weiterschauen" are theirs. The bot uses the
server the household shares (PLEX_URL, otherwise the admin's server, otherwise the first one).
Tokens are stored in data/profiles.db.
"""

from __future__ import annotations

import asyncio
import html
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

PLEX_TV = "https://plex.tv"
LINK_URL = "https://plex.tv/link"
PIN_MINUTES = 15


class PlexError(Exception):
    pass


@dataclass
class Account:
    user_id: int
    token: str  # plex.tv account token
    username: str
    server_url: str
    server_token: str  # this user's token for the server
    machine_id: str
    server_name: str
    linked: str
    synced: str | None = None


@dataclass
class Item:
    kind: str  # movie | series
    title: str
    year: int | None
    rating_key: str
    tmdb: int | None = None
    tvdb: int | None = None
    genres: list[str] | None = None
    watched: bool = False  # movie: seen; show: at least one episode seen
    leaves: int = 0  # show: episodes in Plex
    viewed_leaves: int = 0  # show: episodes this user has seen
    started: bool = False  # movie: started but not finished
    last_viewed: int | None = None  # unix time
    user_rating: float | None = None  # this user's stars in Plex, 0–10

    @property
    def complete(self) -> bool:
        """Seen completely by this user."""
        return self.watched if self.kind == "movie" else bool(self.leaves) and self.viewed_leaves >= self.leaves


@dataclass
class New:
    kind: str  # movie | series
    title: str
    year: int | None
    rating_key: str
    added: int  # unix time
    genres: list[str] = field(default_factory=list)
    episodes: list[str] = field(default_factory=list)  # "S02E03"
    seasons: list[int] = field(default_factory=list)

    @property
    def line(self) -> str:
        name = f"{self.title} ({self.year})" if self.year else self.title
        if self.kind == "movie":
            return f"🎬 {name}"
        if self.seasons:
            return f"📺 {name} – Staffel {', '.join(map(str, sorted(set(self.seasons))))}"
        eps = sorted(set(self.episodes))
        if len(eps) == 1:
            return f"📺 {name} – {eps[0]}"
        return f"📺 {name} – {len(eps)} neue Folgen ({eps[0]}–{eps[-1]})" if eps else f"📺 {name}"


def new_text(items: list[New], like: list[str], avoid: list[str], limit: int = 12,
             norm: Any = None) -> tuple[str, list[New]]:
    """Daily 'Neu in Plex' for one user: avoided genres left out, liked ones first (⭐).
    norm: maps genre names to one spelling ("Comedy" / "Komödie"), default: lower case."""
    def n_(g: str) -> str:
        return ((norm(g) if norm else None) or g).lower()

    low_avoid = {n_(g) for g in avoid}
    low_like = {n_(g) for g in like}

    def genres(n: New) -> set[str]:
        return {n_(g) for g in n.genres}

    keep = [n for n in items if not (genres(n) & low_avoid)]
    if not keep:
        return "", []
    keep.sort(key=lambda n: (not (genres(n) & low_like), -n.added))
    lines = ["🍿 <b>Neu in Plex</b>"]
    for n in keep[:limit]:
        star = " ⭐" if genres(n) & low_like else ""
        lines.append(html.escape(n.line) + star)
    if len(keep) > limit:
        lines.append(f"<i>… und {len(keep) - limit} weitere</i>")
    return "\n".join(lines), keep[:limit]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PlexStore:
    def __init__(self, data_dir: str):
        path = Path(data_dir) / "profiles.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS plex (
                user_id INTEGER PRIMARY KEY, token TEXT NOT NULL, username TEXT, server_url TEXT,
                server_token TEXT, machine_id TEXT, server_name TEXT, linked TEXT, synced TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def client_id(self) -> str:
        """Stable id of this bot towards plex.tv (shows up as one device in the Plex account)."""
        with self._lock, self._db:
            row = self._db.execute("SELECT value FROM meta WHERE key='plex_client_id'").fetchone()
            if row:
                return row["value"]
            cid = "arrbot-" + uuid.uuid4().hex
            self._db.execute("INSERT INTO meta(key, value) VALUES ('plex_client_id', ?)", (cid,))
            return cid

    def save(self, a: Account) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO plex(user_id, token, username, server_url, server_token, machine_id, "
                "server_name, linked, synced) VALUES (?,?,?,?,?,?,?,?,?)",
                (a.user_id, a.token, a.username, a.server_url, a.server_token, a.machine_id, a.server_name,
                 a.linked, a.synced))

    def get(self, user_id: int) -> Account | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM plex WHERE user_id=?", (user_id,)).fetchone()
        return Account(**dict(row)) if row else None

    def all(self) -> list[Account]:
        with self._lock:
            return [Account(**dict(r)) for r in self._db.execute("SELECT * FROM plex")]

    def mark_synced(self, user_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE plex SET synced=? WHERE user_id=?", (utcnow(), user_id))

    def remove(self, user_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM plex WHERE user_id=?", (user_id,))

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self._lock, self._db:
            if value is None:
                self._db.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def _ids(meta: dict) -> tuple[int | None, int | None]:
    tmdb = tvdb = None
    for g in meta.get("Guid") or []:
        gid = str(g.get("id") or "")
        try:
            if gid.startswith("tmdb://"):
                tmdb = int(gid[7:])
            elif gid.startswith("tvdb://"):
                tvdb = int(gid[7:])
        except ValueError:
            pass
    return tmdb, tvdb


def _item(meta: dict) -> Item | None:
    t = meta.get("type")
    if t not in ("movie", "show"):
        return None
    tmdb, tvdb = _ids(meta)
    if t == "movie":
        watched = (meta.get("viewCount") or 0) > 0
    else:
        watched = (meta.get("viewedLeafCount") or 0) > 0
    return Item("movie" if t == "movie" else "series", meta.get("title") or "?", meta.get("year"),
                str(meta.get("ratingKey")), tmdb, tvdb, [g.get("tag") for g in meta.get("Genre") or [] if g.get("tag")],
                watched, int(meta.get("leafCount") or 0), int(meta.get("viewedLeafCount") or 0),
                bool(meta.get("viewOffset")) and not watched, meta.get("lastViewedAt"),
                float(meta["userRating"]) if meta.get("userRating") is not None else None)


class Plex:
    def __init__(self, client_id: str, prefer_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.client_id = client_id
        self.prefer_url = (prefer_url or "").rstrip("/") or None
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(20.0), transport=transport, follow_redirects=True)

    async def close(self) -> None:
        await self._http.aclose()

    def _headers(self, token: str | None = None) -> dict:
        h = {"Accept": "application/json", "X-Plex-Product": "arrbot", "X-Plex-Version": "1.0",
             "X-Plex-Client-Identifier": self.client_id, "X-Plex-Device-Name": "arrbot (Telegram)"}
        if token:
            h["X-Plex-Token"] = token
        return h

    async def _get(self, url: str, token: str | None = None, timeout: float | None = None, **params: Any) -> Any:
        try:
            r = await self._http.get(url, headers=self._headers(token), params=params or None,
                                     timeout=timeout or httpx.USE_CLIENT_DEFAULT)
        except httpx.HTTPError as e:
            raise PlexError(f"Plex nicht erreichbar: {type(e).__name__}") from e
        if r.status_code == 401:
            raise PlexError("Plex lehnt den Zugang ab (401) – bitte neu verbinden: /plex")
        if r.status_code >= 400:
            raise PlexError(f"Plex antwortet mit HTTP {r.status_code}")
        return r.json() if r.content else {}

    # ---- linking --------------------------------------------------------------------------
    async def create_pin(self) -> tuple[int, str]:
        """Short code for plex.tv/link."""
        try:
            r = await self._http.post(f"{PLEX_TV}/api/v2/pins", headers=self._headers(), params={"strong": "false"})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise PlexError(f"plex.tv nicht erreichbar: {type(e).__name__}") from e
        data = r.json()
        return int(data["id"]), str(data["code"])

    async def check_pin(self, pin_id: int) -> str | None:
        data = await self._get(f"{PLEX_TV}/api/v2/pins/{pin_id}")
        return data.get("authToken") or None

    async def username(self, token: str) -> str:
        data = await self._get(f"{PLEX_TV}/api/v2/user", token)
        return data.get("username") or data.get("title") or "Plex"

    async def pick_server(self, token: str, prefer_machine: str | None = None) -> tuple[str, str, str, str]:
        """(url, server token, machine id, name) of the server this account can use."""
        res = await self._get(f"{PLEX_TV}/api/v2/resources", token, includeHttps=1, includeRelay=1)
        servers = [r for r in res or [] if "server" in (r.get("provides") or "")]
        if not servers:
            raise PlexError("Dein Plex-Konto hat keinen Zugriff auf einen Plex-Server.")
        servers.sort(key=lambda r: (r.get("clientIdentifier") != prefer_machine, not r.get("owned")))
        for srv in servers:
            stoken = srv.get("accessToken") or token
            urls = [self.prefer_url] if self.prefer_url else []
            conns = sorted(srv.get("connections") or [], key=lambda c: (bool(c.get("relay")), not c.get("local")))
            urls += [c["uri"] for c in conns if c.get("uri")]
            for url in dict.fromkeys(urls):
                try:
                    ident = await self._get(f"{url}/identity", stoken, timeout=5.0)
                except PlexError:
                    continue
                mid = (ident.get("MediaContainer") or {}).get("machineIdentifier")
                if mid and mid == srv.get("clientIdentifier"):
                    return url, stoken, mid, srv.get("name") or "Plex"
        raise PlexError("Ich erreiche deinen Plex-Server nicht. Tipp: PLEX_URL in der docker-compose.yml setzen "
                        "(z. B. http://192.168.1.10:32400).")

    # ---- server --------------------------------------------------------------------------
    async def _srv(self, a: Account, path: str, **params: Any) -> dict:
        data = await self._get(f"{a.server_url}{path}", a.server_token, **params)
        return data.get("MediaContainer") or {}

    async def library(self, a: Account) -> list[Item]:
        """All movies and shows with this user's watched state."""
        sections = (await self._srv(a, "/library/sections")).get("Directory") or []
        out: list[Item] = []
        for s in sections:
            if s.get("type") not in ("movie", "show"):
                continue
            data = await self._srv(a, f"/library/sections/{s['key']}/all", includeGuids=1)
            out += [i for i in (_item(m) for m in data.get("Metadata") or []) if i]
        return out

    async def sections(self, a: Account) -> list[dict]:
        return (await self._srv(a, "/library/sections")).get("Directory") or []

    async def section_items(self, a: Account, key: str, episodes: bool = False) -> list[dict]:
        """Every title of a library with its versions (Media); episodes=True: all episodes of a show library."""
        params: dict[str, Any] = {"includeGuids": 1}
        if episodes:
            params["type"] = 4
        return (await self._srv(a, f"/library/sections/{key}/all", **params)).get("Metadata") or []

    async def delete_media(self, a: Account, rating_key: str, media_id: int) -> None:
        """Delete one version (file) of a title. Needs "allow media deletion" on the server."""
        try:
            r = await self._http.delete(f"{a.server_url}/library/metadata/{rating_key}/media/{media_id}",
                                        headers=self._headers(a.server_token))
        except httpx.HTTPError as e:
            raise PlexError(f"Plex nicht erreichbar: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise PlexError(f"Plex lässt das Löschen nicht zu (HTTP {r.status_code})")

    async def refresh_libraries(self, a: Account) -> int:
        """Ask Plex to scan all movie and show libraries (finds files Radarr/Sonarr put there)."""
        sections = (await self._srv(a, "/library/sections")).get("Directory") or []
        keys = [s["key"] for s in sections if s.get("type") in ("movie", "show")]
        for k in keys:
            await self._get(f"{a.server_url}/library/sections/{k}/refresh", a.server_token)
        return len(keys)

    async def search(self, a: Account, query: str) -> list[Item]:
        data = await self._srv(a, "/hubs/search", query=query, limit=10, includeGuids=1)
        out = []
        for hub in data.get("Hub") or []:
            out += [i for i in (_item(m) for m in hub.get("Metadata") or []) if i]
        return out

    async def metadata(self, a: Account, rating_key: str) -> dict:
        items = (await self._srv(a, f"/library/metadata/{rating_key}")).get("Metadata") or [{}]
        return items[0]

    async def episodes(self, a: Account, rating_key: str) -> list[dict]:
        return (await self._srv(a, f"/library/metadata/{rating_key}/allLeaves")).get("Metadata") or []

    async def recently_added(self, a: Account, since: int) -> list["New"]:
        """Movies and shows that came into Plex after `since` (unix time); new episodes grouped per show."""
        data = await self._srv(a, "/library/recentlyAdded", includeGuids=1,
                               **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 100})
        out: dict[str, New] = {}
        for m in data.get("Metadata") or []:
            added = int(m.get("addedAt") or 0)
            if added <= since:
                continue
            t = m.get("type")
            if t == "movie":
                out[str(m.get("ratingKey"))] = New("movie", m.get("title") or "?", m.get("year"), str(m.get("ratingKey")),
                                                   added, [g.get("tag") for g in m.get("Genre") or [] if g.get("tag")])
            elif t in ("episode", "season", "show"):
                key = str(m.get("grandparentRatingKey") or m.get("parentRatingKey") or m.get("ratingKey"))                     if t != "show" else str(m.get("ratingKey"))
                title = m.get("grandparentTitle") or m.get("parentTitle") or m.get("title") or "?"
                n = out.setdefault(key, New("series", title, m.get("year") if t == "show" else None, key, added, []))
                n.added = max(n.added, added)
                if t == "episode":
                    n.episodes.append(_ep(m))
                elif t == "season":
                    n.seasons.append(int(m.get("index") or 0))
        return sorted(out.values(), key=lambda n: -n.added)

    async def on_deck(self, a: Account) -> list[dict]:
        return (await self._srv(a, "/library/onDeck")).get("Metadata") or []

    @staticmethod
    def web_link(a: Account, rating_key: str) -> str:
        return (f"https://app.plex.tv/desktop/#!/server/{a.machine_id}/details?key="
                + quote(f"/library/metadata/{rating_key}", safe=""))


async def wait_for_pin(plex: Plex, pin_id: int, every: float = 3.0, minutes: int = PIN_MINUTES) -> str | None:
    """Poll plex.tv until the code was entered (tests pass a small `every`)."""
    for _ in range(int(minutes * 60 / every)):
        token = await plex.check_pin(pin_id)
        if token:
            return token
        await asyncio.sleep(every)
    return None


# ---- texts ------------------------------------------------------------------------------------
def _ep(e: dict) -> str:
    return f"S{e.get('parentIndex', 0):02d}E{e.get('index', 0):02d}"


def _mins(ms: int | None) -> int:
    return round((ms or 0) / 60000)


def progress_text(item: Item, meta: dict, episodes: list[dict]) -> tuple[str, str | None]:
    """(text, rating key to link) for 'Wo war ich bei X?'."""
    name = f"{item.title} ({item.year})" if item.year else item.title
    if item.kind == "movie":
        if (meta.get("viewCount") or 0) > 0:
            when = meta.get("lastViewedAt")
            day = f" am {datetime.fromtimestamp(when):%d.%m.%Y}" if when else ""
            return f"🎬 {name}: schon gesehen{day}. ✅", item.rating_key
        if meta.get("viewOffset"):
            return (f"🎬 {name}: angefangen, du bist bei {_mins(meta['viewOffset'])} von "
                    f"{_mins(meta.get('duration'))} Minuten.", item.rating_key)
        return f"🎬 {name} ist in Plex, aber noch nicht gesehen.", item.rating_key
    eps = [e for e in episodes if (e.get("parentIndex") or 0) > 0]
    seen = [e for e in eps if (e.get("viewCount") or 0) > 0]
    started = next((e for e in eps if e.get("viewOffset") and not e.get("viewCount")), None)
    last = max((i for i, e in enumerate(eps) if (e.get("viewCount") or 0) > 0), default=-1)
    nxt = started or next((e for e in eps[last + 1:] if not e.get("viewCount")), None)
    lines = [f"📺 {name}: {len(seen)} von {len(eps)} Folgen gesehen"]
    if not seen and not started:
        lines = [f"📺 {name} ist in Plex ({len(eps)} Folgen), du hast noch nicht angefangen."]
    if nxt:
        title = f" „{nxt.get('title')}“" if nxt.get("title") else ""
        at = f" – bei {_mins(nxt['viewOffset'])} von {_mins(nxt.get('duration'))} min" if nxt.get("viewOffset") else ""
        lines.append(f"▶️ {'Weiter mit' if seen or started else 'Los geht’s mit'} {_ep(nxt)}{title}{at}")
        return "\n".join(lines), str(nxt.get("ratingKey") or item.rating_key)
    if eps and len(seen) == len(eps):
        lines.append("✅ Alles gesehen!")
    return "\n".join(lines), item.rating_key


def on_deck_text(deck: list[dict]) -> str:
    if not deck:
        return "▶️ Gerade schaust du nichts angefangenes in Plex."
    out = ["▶️ <b>Weiterschauen in Plex</b>"]
    for m in deck[:8]:
        if m.get("type") == "episode":
            line = f"📺 {m.get('grandparentTitle')} {_ep(m)}" + (f" „{m.get('title')}“" if m.get("title") else "")
        else:
            line = f"🎬 {m.get('title')}" + (f" ({m['year']})" if m.get("year") else "")
        out.append(html.escape(line, quote=False))
    return "\n".join(out)


ABANDONED_DAYS = 60  # started, then not touched for so long: probably not their thing
FRESH_DAYS = 2  # watched so recently that "wie fandest du's?" still makes sense


def abandoned(it: Item, now: int) -> bool:
    if not it.last_viewed or now - it.last_viewed < ABANDONED_DAYS * 86400:
        return False
    if it.kind == "movie":
        return it.started
    return it.leaves >= 6 and 0 < it.viewed_leaves <= it.leaves * 0.25


async def sync(plex: Plex, store: PlexStore, profiles: Any, a: Account, fresh: list[Item] | None = None,
               now: int | None = None) -> int:
    """Learn what this user watched in Plex (only new titles count), their stars (⭐4–5 = 👍, ⭐1–2 = 👎) and
    what they gave up on. Returns how many were newly watched; fresh gets the ones seen in the last days."""
    now = now or int(datetime.now(timezone.utc).timestamp())
    known = {a_: profiles.known(a.user_id, a_) for a_ in ("watched", "liked", "disliked", "abandoned")}
    new = 0
    for it in await plex.library(a):
        if not (it.tmdb or it.tvdb):
            continue
        key = (it.kind, it.tmdb, it.tvdb if it.kind == "series" else None)
        name = f"{it.title} ({it.year})" if it.kind == "movie" and it.year else it.title
        args = dict(tmdb=it.tmdb, tvdb=it.tvdb if it.kind == "series" else None, year=it.year,
                    genres=it.genres or [])
        if it.user_rating is not None:
            stars = it.user_rating / 2
            action = "liked" if stars >= 4 else "disliked" if stars <= 2 else None
            if action and key not in known[action]:
                profiles.learn(a.user_id, action, it.kind, name, **args)
        elif abandoned(it, now) and key not in known["abandoned"] and key not in known["liked"]:
            profiles.learn(a.user_id, "abandoned", it.kind, name, **args)
        if not it.watched or key in known["watched"]:
            continue
        profiles.learn(a.user_id, "watched", it.kind, name, **args)
        new += 1
        if (fresh is not None and it.user_rating is None and it.last_viewed
                and now - it.last_viewed <= FRESH_DAYS * 86400 and (it.kind == "movie" or it.complete)):
            fresh.append(it)
    store.mark_synced(a.user_id)
    return new
