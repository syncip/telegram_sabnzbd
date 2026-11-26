"""TMDB (themoviedb.org): German titles, German release dates, streaming in Germany, recommendations.

Free API key: themoviedb.org → Einstellungen → API. Both the short v3 "API-Schlüssel" and the
long v4 "Lesezugriffs-Token" work. Results are cached in memory for a few hours.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from .profiles import TMDB_GENRES

log = logging.getLogger(__name__)

LANG, REGION = "de-DE", "DE"
_GENERIC_TITLE = re.compile(r"^(folge|episode|episodio|épisode)\s*\d+$", re.I)
CACHE_SECONDS = 6 * 3600
# TMDB release types
PREMIERE, THEATRICAL_LIMITED, THEATRICAL, DIGITAL, PHYSICAL, TV = 1, 2, 3, 4, 5, 6


class TmdbError(Exception):
    pass


@dataclass
class GermanInfo:
    """What we know about a title in Germany."""

    kind: str  # movie | series
    tmdb_id: int
    title_de: str | None
    original_title: str | None
    original_language: str | None  # ISO 639-1, e.g. "en"
    has_german_text: bool  # TMDB has a German title/overview (≠ proof of a German dub)
    cinema: date | None = None
    digital: date | None = None
    disc: date | None = None
    tv: date | None = None
    streaming: list[str] = field(default_factory=list)  # flatrate providers in DE
    buy_rent: list[str] = field(default_factory=list)
    first_air: date | None = None  # series: original premiere
    overview_de: str | None = None

    @property
    def german_original(self) -> bool:
        return self.original_language == "de"

    def to_dict(self) -> dict:
        """Compact form for the AI (few tokens)."""
        d = {
            "de_title": self.title_de,
            "orig_lang": self.original_language,
            "kino_de": _iso(self.cinema),
            "digital_de": _iso(self.digital),
            "disc_de": _iso(self.disc),
            "tv_de": _iso(self.tv),
            "streaming_de": self.streaming[:5],
            "kaufen_leihen_de": self.buy_rent[:3],
            "de_text": self.has_german_text,
        }
        return {k: v for k, v in d.items() if v not in (None, [], "")}


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


@dataclass
class Recommendation:
    kind: str
    tmdb_id: int
    title: str
    year: int | None
    rating: float
    votes: int
    score: float = 0.0
    because: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)  # German names


class TMDB:
    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        headers, self._params = {}, {}
        if api_key.startswith("eyJ"):  # v4 read access token (JWT)
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            self._params["api_key"] = api_key
        self._http = httpx.AsyncClient(
            base_url="https://api.themoviedb.org/3", headers=headers, timeout=httpx.Timeout(15.0), transport=transport
        )
        self._cache: dict[str, tuple[float, Any]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        key = f"{path}?{sorted(params.items())}"
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        try:
            r = await self._http.get(path, params={**self._params, **params})
        except httpx.HTTPError as e:
            raise TmdbError(f"TMDB nicht erreichbar: {e}") from e
        if r.status_code == 401:
            raise TmdbError("TMDB lehnt den API-Key ab (TMDB_API_KEY prüfen).")
        if r.status_code == 404:
            raise TmdbError("Bei TMDB nicht gefunden.")
        if r.status_code >= 400:
            raise TmdbError(f"TMDB antwortet mit HTTP {r.status_code}")
        data = r.json()
        self._cache[key] = (time.monotonic(), data)
        if len(self._cache) > 2000:
            for k in list(self._cache)[:500]:
                self._cache.pop(k, None)
        return data

    # ---- ids ------------------------------------------------------------------
    async def tv_from_tvdb(self, tvdb_id: int) -> int | None:
        data = await self._get(f"/find/{tvdb_id}", external_source="tvdb_id")
        res = data.get("tv_results") or []
        return res[0]["id"] if res else None

    async def tvdb_from_tv(self, tmdb_id: int) -> int | None:
        data = await self._get(f"/tv/{tmdb_id}/external_ids")
        return data.get("tvdb_id")

    # ---- German titles ---------------------------------------------------------
    async def german_title(self, kind: str, tmdb_id: int) -> str | None:
        path = f"/movie/{tmdb_id}" if kind == "movie" else f"/tv/{tmdb_id}"
        data = await self._get(path, language=LANG)
        return data.get("title") or data.get("name")

    async def german_titles(self, kind: str, ids: list[int]) -> dict[int, str]:
        async def one(i: int) -> tuple[int, str | None]:
            try:
                return i, await self.german_title(kind, i)
            except TmdbError:
                return i, None

        return {i: t for i, t in await asyncio.gather(*(one(i) for i in ids)) if t}

    # ---- German availability ----------------------------------------------------
    async def german_info(self, kind: str, tmdb_id: int) -> GermanInfo:
        if kind == "movie":
            data = await self._get(
                f"/movie/{tmdb_id}", language=LANG, append_to_response="release_dates,watch/providers,translations"
            )
            info = GermanInfo(
                "movie", tmdb_id, data.get("title"), data.get("original_title"), data.get("original_language"),
                _has_german(data), overview_de=data.get("overview") or None,
            )
            for country in (data.get("release_dates") or {}).get("results") or []:
                if country.get("iso_3166_1") != REGION:
                    continue
                for rd in country.get("release_dates") or []:
                    d, t = _date(rd.get("release_date")), rd.get("type")
                    if t in (THEATRICAL, THEATRICAL_LIMITED, PREMIERE):
                        info.cinema = min(filter(None, (info.cinema, d)), default=None)
                    elif t == DIGITAL:
                        info.digital = min(filter(None, (info.digital, d)), default=None)
                    elif t == PHYSICAL:
                        info.disc = min(filter(None, (info.disc, d)), default=None)
                    elif t == TV:
                        info.tv = min(filter(None, (info.tv, d)), default=None)
        else:
            data = await self._get(
                f"/tv/{tmdb_id}", language=LANG, append_to_response="watch/providers,translations"
            )
            info = GermanInfo(
                "series", tmdb_id, data.get("name"), data.get("original_name"), data.get("original_language"),
                _has_german(data), first_air=_date(data.get("first_air_date")), overview_de=data.get("overview") or None,
            )
        prov = ((data.get("watch/providers") or {}).get("results") or {}).get(REGION) or {}
        info.streaming = [p["provider_name"] for p in prov.get("flatrate") or []]
        info.buy_rent = list(dict.fromkeys(p["provider_name"] for p in (prov.get("rent") or []) + (prov.get("buy") or [])))
        return info

    async def season_titles(self, tmdb_id: int, season: int) -> dict[int, dict]:
        """{episode: {"de": German title or None, "en": title, "air": date}} for one season."""
        de = await self._get(f"/tv/{tmdb_id}/season/{season}", language=LANG)
        en = await self._get(f"/tv/{tmdb_id}/season/{season}", language="en-US")
        en_names = {e.get("episode_number"): e.get("name") or "" for e in en.get("episodes") or []}
        out = {}
        for e in de.get("episodes") or []:
            n = e.get("episode_number")
            name, english = (e.get("name") or "").strip(), en_names.get(n, "")
            generic = not name or bool(_GENERIC_TITLE.match(name))
            out[n] = {
                # A real German title (not "Folge 5", not just the English one) hints at a German broadcast.
                "de": None if generic or name == english else name,
                "en": english,
                "air": _date(e.get("air_date")),
            }
        return out

    # ---- suggestions ----------------------------------------------------------------
    async def recommendations(self, kind: str, tmdb_id: int) -> list[Recommendation]:
        path = f"/movie/{tmdb_id}/recommendations" if kind == "movie" else f"/tv/{tmdb_id}/recommendations"
        data = await self._get(path, language=LANG)
        return [_rec(kind, r) for r in data.get("results") or []]

    async def companies(self, name: str) -> list[int]:
        """Studio ids for a name ("Marvel" -> Marvel Studios, Marvel Entertainment ...)."""
        data = await self._get("/search/company", query=name)
        want = name.lower().strip()
        hits = [r for r in data.get("results") or [] if want in (r.get("name") or "").lower()]
        return [r["id"] for r in (hits or data.get("results") or [])[:4]]

    async def top(self, kind: str, start: date | None, end: date | None, sort: str = "popular",
                  companies: list[int] | None = None) -> list[Recommendation]:
        """Top titles released between start and end (None: all time), optionally of some studios.
        sort: popular (most votes) | rating | revenue."""
        field_from, field_to = (
            ("primary_release_date.gte", "primary_release_date.lte") if kind == "movie"
            else ("first_air_date.gte", "first_air_date.lte")
        )
        order = {"rating": "vote_average.desc", "revenue": "revenue.desc"}.get(sort, "vote_count.desc")
        if kind != "movie" and order == "revenue.desc":
            order = "vote_count.desc"
        params: dict[str, Any] = {
            "sort_by": order,
            "language": LANG, "include_adult": "false",
            # Ratings need enough votes, otherwise obscure titles with 3 votes of 10/10 win.
            "vote_count.gte": 200 if order == "vote_average.desc" else 10,
        }
        if start:
            params[field_from] = start.isoformat()
        if end:
            params[field_to] = end.isoformat()
        if companies:
            params["with_companies"] = "|".join(map(str, companies))
        if kind != "movie":
            params["without_genres"] = "10763,10767"  # news, talk shows
        data = await self._get(f"/discover/{'movie' if kind == 'movie' else 'tv'}", **params)
        return [_rec(kind, r) for r in data.get("results") or []]

    async def details(self, kind: str, tmdb_id: int) -> dict:
        """Runtime (minutes; series: per episode) and film series (collection id) of a title."""
        data = await self._get(f"/{'movie' if kind == 'movie' else 'tv'}/{tmdb_id}", language=LANG)
        if kind == "movie":
            runtime = data.get("runtime") or None
        else:
            runs = data.get("episode_run_time") or []
            runtime = min(runs) if runs else None
        coll = (data.get("belongs_to_collection") or {}).get("id")
        return {"runtime": runtime, "collection": coll, "overview": data.get("overview") or ""}

    async def discover(self, kind: str, genre_ids: list[int], max_runtime: int | None = None,
                       from_year: int | None = None, to_year: int | None = None,
                       min_rating: float | None = None, page: int = 1) -> list[Recommendation]:
        """Well rated titles of some genres ("was Lustiges unter 90 Minuten")."""
        f_from, f_to = (("primary_release_date.gte", "primary_release_date.lte") if kind == "movie"
                        else ("first_air_date.gte", "first_air_date.lte"))
        params: dict[str, Any] = {"sort_by": "vote_average.desc", "vote_count.gte": 300, "language": LANG,
                                  "include_adult": "false", "page": page}
        if genre_ids:
            params["with_genres"] = "|".join(map(str, genre_ids))
        if max_runtime:
            params["with_runtime.lte"] = int(max_runtime)
        if from_year:
            params[f_from] = f"{int(from_year)}-01-01"
        if to_year:
            params[f_to] = f"{int(to_year)}-12-31"
        if min_rating:
            params["vote_average.gte"] = float(min_rating)
        if kind != "movie":
            params["without_genres"] = "10763,10767"
        data = await self._get(f"/discover/{'movie' if kind == 'movie' else 'tv'}", **params)
        return [_rec(kind, r) for r in data.get("results") or []]

    async def trending(self, kind: str) -> list[Recommendation]:
        data = await self._get(f"/trending/{'movie' if kind == 'movie' else 'tv'}/week", language=LANG)
        return [_rec(kind, r) for r in data.get("results") or []]


def _has_german(data: dict) -> bool:
    for t in (data.get("translations") or {}).get("translations") or []:
        if t.get("iso_639_1") == "de":
            d = t.get("data") or {}
            if d.get("overview") or d.get("title") or d.get("name"):
                return True
    return data.get("original_language") == "de"


def _rec(kind: str, r: dict) -> Recommendation:
    d = _date(r.get("release_date") or r.get("first_air_date"))
    return Recommendation(
        kind, r["id"], r.get("title") or r.get("name") or "?", d.year if d else None,
        float(r.get("vote_average") or 0), int(r.get("vote_count") or 0),
        genres=list(dict.fromkeys(TMDB_GENRES[g] for g in r.get("genre_ids") or [] if g in TMDB_GENRES)),
    )


def german_summary(info: GermanInfo, today: date) -> str | None:
    """One line for messages, e.g. '🇩🇪 Deutsch erst ab 12.03.2027 digital'. None if nothing worth saying."""
    f = lambda d: d.strftime("%d.%m.%Y")  # noqa: E731
    if info.german_original:
        return None
    if info.kind == "movie":
        home = min(filter(None, (info.digital, info.disc)), default=None)
        if home and home > today:
            cinema = f", im Kino seit {f(info.cinema)}" if info.cinema and info.cinema <= today else ""
            return f"🇩🇪 Auf Deutsch zu Hause erst ab {f(home)} ({'digital' if home == info.digital else 'Disc'}){cinema}."
        if info.cinema and info.cinema > today:
            return f"🇩🇪 Deutscher Kinostart erst am {f(info.cinema)}. Eine deutsche Fassung zum Download kommt meist Monate später."
        if info.cinema and not home and (today - info.cinema).days < 120:
            return f"🇩🇪 Läuft seit {f(info.cinema)} in deutschen Kinos, noch kein deutscher Digital-/Disc-Termin."
        if not (info.cinema or home or info.streaming or info.buy_rent):
            return "🇩🇪 Für Deutschland ist bei TMDB keine Veröffentlichung bekannt – evtl. gibt es (noch) keine deutsche Fassung."
        return None
    if not (info.streaming or info.buy_rent or info.has_german_text):
        return "🇩🇪 In Deutschland bisher nirgends verfügbar – eine deutsche Synchro gibt es vermutlich (noch) nicht."
    return None
