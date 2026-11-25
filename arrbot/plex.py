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
from dataclasses import dataclass
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

    @property
    def complete(self) -> bool:
        """Seen completely by this user."""
        return self.watched if self.kind == "movie" else bool(self.leaves) and self.viewed_leaves >= self.leaves


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
                bool(meta.get("viewOffset")) and not watched, meta.get("lastViewedAt"))


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


async def sync(plex: Plex, store: PlexStore, profiles: Any, a: Account) -> int:
    """Learn what this user watched in Plex (only new titles count). Returns how many were new."""
    known = profiles.known(a.user_id, "watched")
    new = 0
    for it in await plex.library(a):
        if not it.watched or (it.kind, it.tmdb, it.tvdb if it.kind == "series" else None) in known:
            continue
        if not (it.tmdb or it.tvdb):
            continue
        name = f"{it.title} ({it.year})" if it.kind == "movie" and it.year else it.title
        profiles.learn(a.user_id, "watched", it.kind, name, tmdb=it.tmdb,
                       tvdb=it.tvdb if it.kind == "series" else None, year=it.year, genres=it.genres or [])
        new += 1
    store.mark_synced(a.user_id)
    return new
