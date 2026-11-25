"""📚 Plex collections: "Mach eine Sammlung ‚Marvel‘" and automatic "Wünsche von Anna".

Collections live on the Plex server and everyone with access sees them. They are made with the token
of an admin who linked Plex (/plex) and owns or manages the server.
  - Named collection: the AI only lists the titles it means; matching to the library happens here.
  - Wish collections (on/off): once a day every user's requests that are in Plex go into
    "Wünsche von <Name>". Titles are only added, never removed (Plex edits stay untouched).
Movies and shows live in different libraries, so one collection per library is made where needed.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .plex import Account, Item, Plex, PlexError

log = logging.getLogger(__name__)

TYPE = {"movie": 1, "show": 2}


def norm(title: str) -> str:
    t = unicodedata.normalize("NFKD", title.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", " ", t.replace("&", " and "))
    return " ".join(w for w in t.split() if w not in ("the", "der", "die", "das", "a"))


@dataclass
class Result:
    name: str
    added: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    created: bool = False

    def text(self) -> str:
        if not self.added and not self.missing:
            return f"📚 „{self.name}“: nichts zu tun."
        verb = "angelegt" if self.created else "ergänzt"
        out = [f"📚 Sammlung „{self.name}“ {verb}: {len(self.added)} Titel" + (" neu dazu" if not self.created else "")]
        if self.added:
            out.append(", ".join(self.added[:15]) + (" …" if len(self.added) > 15 else ""))
        if self.missing:
            out.append(f"Nicht in Plex ({len(self.missing)}): " + ", ".join(self.missing[:10])
                       + (" …" if len(self.missing) > 10 else ""))
        return "\n".join(out)


def match(items: list[Item], wanted: list[dict]) -> tuple[list[Item], list[str]]:
    """wanted: [{"title", "year"?}] -> (library items, titles not found)."""
    by_title: dict[str, list[Item]] = {}
    for i in items:
        by_title.setdefault(norm(i.title), []).append(i)
    found, missing = [], []
    for w in wanted:
        hits = by_title.get(norm(w.get("title") or ""), [])
        year = w.get("year")
        if year:
            hits = [h for h in hits if not h.year or abs(h.year - int(year)) <= 1] or []
        if hits:
            found.append(hits[0])
        else:
            missing.append(w.get("title") or "?")
    return list({i.rating_key: i for i in found}.values()), missing


class Collections:
    def __init__(self, plex: Plex):
        self.plex = plex

    async def _sections(self, a: Account) -> list[dict]:
        return [s for s in (await self.plex._srv(a, "/library/sections")).get("Directory") or []
                if s.get("type") in TYPE]

    async def _existing(self, a: Account, section: str, name: str) -> str | None:
        data = await self.plex._srv(a, f"/library/sections/{section}/collections")
        for c in data.get("Metadata") or []:
            if (c.get("title") or "").lower() == name.lower():
                return str(c.get("ratingKey"))
        return None

    async def _children(self, a: Account, coll: str) -> set[str]:
        data = await self.plex._srv(a, f"/library/collections/{coll}/children")
        return {str(m.get("ratingKey")) for m in data.get("Metadata") or []}

    def _uri(self, a: Account, keys: list[str]) -> str:
        return f"server://{a.machine_id}/com.plexapp.plugins.library/library/metadata/{','.join(keys)}"

    async def _send(self, method: str, a: Account, path: str, **params: Any) -> dict:
        try:
            r = await self.plex._http.request(method, f"{a.server_url}{path}", headers=self.plex._headers(a.server_token),
                                              params=params)
        except Exception as e:  # noqa: BLE001
            raise PlexError(f"Plex nicht erreichbar: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise PlexError("Plex erlaubt das nicht – Sammlungen kann nur der Server-Besitzer anlegen.")
        if r.status_code >= 400:
            raise PlexError(f"Plex antwortet mit HTTP {r.status_code}")
        return (r.json() if r.content else {}) or {}

    async def put(self, a: Account, name: str, items: list[Item], missing: list[str] | None = None) -> Result:
        """Make sure the collection exists in every library that has items, and holds them all."""
        res = Result(name, missing=list(missing or []))
        sections = {s["type"]: str(s["key"]) for s in await self._sections(a)}
        for ptype, kind in (("movie", "movie"), ("show", "series")):
            mine = [i for i in items if i.kind == kind]
            if not mine or ptype not in sections:
                continue
            section = sections[ptype]
            coll = await self._existing(a, section, name)
            if coll is None:
                keys = [i.rating_key for i in mine]
                await self._send("POST", a, "/library/collections", type=TYPE[ptype], title=name, smart=0,
                                 sectionId=section, uri=self._uri(a, keys))
                res.created = True
                res.added += [i.title for i in mine]
                continue
            have = await self._children(a, coll)
            new = [i for i in mine if i.rating_key not in have]
            if new:
                await self._send("PUT", a, f"/library/collections/{coll}/items", uri=self._uri(a, [i.rating_key for i in new]))
                res.added += [i.title for i in new]
        return res


def wish_items(requests: list[dict], user_id: int, library: list[Item]) -> list[Item]:
    """Library items this user asked for (requests from the bot's state)."""
    want_movies = {r.get("tmdb") for r in requests if r.get("chat_id") == user_id and r.get("kind") == "movie"}
    want_series = {r.get("tvdb") for r in requests if r.get("chat_id") == user_id and r.get("kind") == "series"}
    return [i for i in library if (i.kind == "movie" and i.tmdb in want_movies)
            or (i.kind == "series" and i.tvdb in want_series)]
