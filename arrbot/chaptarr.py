"""📚 Chaptarr (the Readarr successor for ebooks and audiobooks): find a book, add it, search it, its files.

Chaptarr speaks Readarr's /api/v1, extended: one book has separate rows per media type (ebook / audiobook),
and a new book is added with POST /book/import (author and book in one go). There is no endpoint that
hands out the file itself, so the bot reads it from the library folder it shares with Chaptarr (a
read-only Docker mount, paths translated with BOOK_PATHS "chaptarr path=bot path").
"""

from __future__ import annotations

import logging
from typing import Any

from .arr import ArrError, _ArrBase
from .config import ArrConfig

log = logging.getLogger(__name__)

MEDIA = "ebook"  # what can be sent in Telegram (audiobooks are hundreds of MB)


class Chaptarr(_ArrBase):
    kind = "Chaptarr"

    def __init__(self, cfg: ArrConfig, transport: Any = None):
        super().__init__(cfg, transport)
        self._http.base_url = f"{cfg.url}/api/v1"

    async def lookup(self, term: str, media: str = MEDIA) -> list[dict]:
        return await self._req("GET", "/book/lookup", params={"term": term, "mediaType": media}, timeout=60.0) or []

    async def book(self, book_id: int) -> dict:
        return await self._req("GET", f"/book/{book_id}") or {}

    async def files(self, book_id: int, media: str = MEDIA) -> list[dict]:
        return await self._req("GET", "/bookfile", params={"bookId": book_id, "mediaType": media}) or []

    async def defaults(self, media: str = MEDIA) -> tuple[str, int, int]:
        """Root folder, quality and metadata profile for new books of this media type."""
        folders = await self._req("GET", "/rootfolder") or []
        folder = (next((f for f in folders if f.get(f"isEffectiveDefault{media.capitalize()}")), None)
                  or next((f for f in folders if f.get(media)), None) or (folders[0] if folders else None))
        if not folder:
            raise ArrError("In Chaptarr ist kein Bibliotheksordner (Root Folder) angelegt.")
        if self.cfg.root_folder:
            folder = next((f for f in folders if f.get("path") == self.cfg.root_folder), folder)
        qp = folder.get(f"{media}QualityProfileId")
        mp = folder.get(f"{media}MetadataProfileId")
        if not qp:
            profiles = await self._req("GET", "/qualityprofile") or []
            want = self.cfg.quality_profile
            prof = (next((p for p in profiles if want and p.get("name") == want), None)
                    or next((p for p in profiles if p.get("profileType") == media), None) or (profiles[0] if profiles else {}))
            qp = prof.get("id")
        if not mp:
            meta = await self._req("GET", "/metadataprofile") or []
            mp = meta[0].get("id") if meta else None
        if not qp:
            raise ArrError("In Chaptarr ist kein Qualitätsprofil für E-Books angelegt.")
        return folder["path"], int(qp), int(mp or 0)

    async def add(self, hit: dict, media: str = MEDIA) -> int | None:
        """Add a book from a lookup result. Returns its id, or None while Chaptarr still loads the author."""
        root, qp, mp = await self.defaults(media)
        author = hit.get("author") or {}
        edition = hit.get("foreignEditionId") or next(
            (e.get("foreignEditionId") for e in hit.get("editions") or [] if e.get("monitored")), None) or next(
            (e.get("foreignEditionId") for e in hit.get("editions") or []), None)
        body = {
            "foreignBookId": hit.get("foreignBookId"), "foreignAuthorId": author.get("foreignAuthorId"),
            "authorName": author.get("authorName") or hit.get("authorTitle"), "foreignEditionId": edition,
            "mediaType": media, "rootFolder": root, "qualityProfileId": qp, "metadataProfileId": mp or None,
            # only this book: the author's other books stay unmonitored, nothing else is searched
            "authorMonitoring": {"monitor": "none", "monitorFuture": False, f"{media}Monitored": True,
                                 "searchForMissing": False},
        }
        res = await self._req("POST", "/book/import", json=body)
        if isinstance(res, dict) and res.get("id"):
            return int(res["id"])
        if isinstance(res, dict) and res.get("pendingId"):
            return None
        return await self.find(hit, media)

    async def find(self, hit: dict, media: str = MEDIA) -> int | None:
        """The library id of a lookup result (after adding it)."""
        if hit.get("titleSlug"):
            for b in await self._req("GET", "/book", params={"titleSlug": hit["titleSlug"], "mediaType": media}) or []:
                if b.get("id"):
                    return int(b["id"])
        for b in await self.lookup(hit.get("title") or "", media):
            if b.get("id") and b.get("foreignBookId") == hit.get("foreignBookId"):
                return int(b["id"])
        return None

    async def want(self, book_id: int) -> None:
        """Monitor the book and search it now."""
        await self._req("PUT", "/book/monitor", json={"bookIds": [book_id], "monitored": True})
        await self._req("POST", "/command", json={"name": "BookSearch", "bookIds": [book_id]})
