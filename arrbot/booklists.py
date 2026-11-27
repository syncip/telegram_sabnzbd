"""📈 Book lists from Open Library (free, no key): new releases per genre and language, and what is trending.

Feeds the AI facts it cannot know by itself ("aktuelle Krimis"): it picks from these lists and the bot then
checks every pick in Chaptarr.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

BASE = "https://openlibrary.org"
HEADERS = {"User-Agent": "arrbot (self-hosted Telegram bot)"}
# Genre words the AI may pass -> Open Library subjects
SUBJECTS = {
    "crime": "crime", "krimi": "crime", "thriller": "thriller", "mystery": "mystery",
    "science_fiction": "science_fiction", "scifi": "science_fiction", "fantasy": "fantasy",
    "romance": "romance", "horror": "horror", "historical_fiction": "historical_fiction",
    "biography": "biography", "humor": "humor", "young_adult": "young_adult_fiction",
    "children": "juvenile_fiction", "dystopia": "dystopias", "nonfiction": "nonfiction",
}


def _row(doc: dict) -> dict:
    return {"title": doc.get("title") or "?", "author": ", ".join((doc.get("author_name") or [])[:2]),
            "year": doc.get("first_publish_year")}


async def _get(path: str, params: dict, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    async with httpx.AsyncClient(base_url=BASE, timeout=25.0, headers=HEADERS, transport=transport) as c:
        r = await c.get(path, params=params)
        r.raise_for_status()
        return r.json()


async def new_books(subject: str | None = None, lang: str = "ger", years: int = 2, limit: int = 25,
                    transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Newest books of a genre that exist in this language (first published in the last `years` years)."""
    q = " ".join(x for x in (f"subject:{SUBJECTS.get((subject or '').lower(), subject)}" if subject else "",
                              f"language:{lang}" if lang else "") if x) or "*"
    data = await _get("/search.json", {"q": q, "sort": "new", "limit": 100,
                                       "fields": "title,author_name,first_publish_year"}, transport)
    since = datetime.now().year - years + 1
    return [_row(d) for d in data.get("docs") or [] if (d.get("first_publish_year") or 0) >= since][:limit]


async def trending(period: str = "weekly", lang: str = "ger", limit: int = 25,
                   transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """What Open Library users look at most (daily/weekly/monthly/yearly), with an edition in this language."""
    period = period if period in ("daily", "weekly", "monthly", "yearly") else "weekly"
    data = await _get(f"/trending/{period}.json", {"limit": 200}, transport)
    works = data.get("works") or []
    return [_row(w) for w in works if not lang or lang in (w.get("language") or [])][:limit]
