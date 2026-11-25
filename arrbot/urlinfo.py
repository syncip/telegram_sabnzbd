"""Identify a movie/series from a link (IMDb, TMDB, TVDB, Trakt, Letterboxd, ...).

IDs in the URL itself are used first. Otherwise the page is fetched and only its
metadata (title, description, JSON-LD, links to IMDb/TMDB/TVDB) is extracted, so
page text never reaches the language model as free-form content.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from collections import Counter
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

MAX_BYTES = 1_500_000
MAX_REDIRECTS = 5
UA = "Mozilla/5.0 (compatible; arrbot/1.0; +https://github.com/)"

RE_IMDB = re.compile(r"imdb\.com/(?:[a-z]{2}/)?title/(tt\d{5,10})")
RE_TMDB = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)")
RE_TVDB_ID = re.compile(r"thetvdb\.com/[^\s\"']*?[?&]id=(\d+)")
RE_TT_ANY = re.compile(r"\b(tt\d{7,10})\b")
GENERIC_SEGMENTS = {
    "title", "movie", "movies", "film", "filme", "tv", "show", "shows", "series", "serie", "serien",
    "wiki", "watch", "details", "info", "overview", "seasons", "season",
}


class UrlError(Exception):
    pass


def ids_from_text(text: str) -> dict:
    """IDs that appear in a URL or in links on a page."""
    ids: dict = {}
    if m := RE_IMDB.search(text):
        ids["imdb"] = m.group(1)
    for kind, num in RE_TMDB.findall(text):
        ids.setdefault("tmdb_movie" if kind == "movie" else "tmdb_tv", int(num))
    if m := RE_TVDB_ID.search(text):
        ids["tvdb"] = int(m.group(1))
    return ids


def _meta(page: str, name: str) -> str | None:
    pat = rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']*)["\']'
    m = re.search(pat, page, re.I) or re.search(
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(name)}["\']', page, re.I
    )
    return html.unescape(m.group(1)).strip() if m else None


def _json_ld(page: str) -> dict:
    for raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict) and it.get("@type") in ("Movie", "TVSeries", "TVSeason", "TVEpisode"):
                return it
    return {}


def parse_page(page: str, url: str) -> dict:
    ld = _json_ld(page)
    title = ld.get("name") or _meta(page, "og:title")
    page_title = None
    if not title and (m := re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)):
        # Plain <title> is often just the site name on app-like sites (e.g. Trakt), so keep it apart.
        page_title = html.unescape(m.group(1)).strip()[:200] or None
    date = ld.get("datePublished") or ld.get("startDate") or ld.get("dateCreated") or ""
    year = int(date[:4]) if re.match(r"\d{4}", str(date)) else None
    og_type = (_meta(page, "og:type") or "").lower()
    kind = {"Movie": "movie", "TVSeries": "series", "TVSeason": "series", "TVEpisode": "series"}.get(ld.get("@type", ""))
    if not kind and "movie" in og_type:
        kind = "movie"
    elif not kind and ("tv_show" in og_type or "episode" in og_type):
        kind = "series"

    ids = ids_from_text(page)
    same_as = ld.get("sameAs")
    if same_as:
        ids = {**ids_from_text(" ".join(same_as if isinstance(same_as, list) else [same_as])), **ids}
    if "imdb" not in ids:
        # Pick the most frequent IMDb id on the page (ignores "related" titles mentioned once).
        common = Counter(RE_TT_ANY.findall(page)).most_common(1)
        if common and common[0][1] >= 2:
            ids["imdb"] = common[0][0]
    desc = ld.get("description") or _meta(page, "og:description") or _meta(page, "description") or ""
    return {
        "title": (title or "")[:200] or None,
        "page_title": page_title,
        "year": year,
        "kind": kind,
        "description": html.unescape(desc)[:300] or None,
        "ids": ids,
    }


async def _check_public(host: str) -> None:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UrlError(f"Adresse {host} nicht gefunden") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise UrlError("Links ins lokale Netz werden nicht geöffnet")


async def fetch(url: str, transport: httpx.AsyncBaseTransport | None = None, check_host: bool = True) -> tuple[str, str]:
    """Fetch a public web page. Returns (final_url, html)."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        headers={"User-Agent": UA, "Accept-Language": "de,en;q=0.8"},
        transport=transport,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlparse(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise UrlError("Nur http- und https-Links werden unterstützt")
            if check_host:
                await _check_public(parts.hostname)
            async with client.stream("GET", url) as r:
                if r.is_redirect and "location" in r.headers:
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise UrlError(f"Seite antwortet mit HTTP {r.status_code}")
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                return url, body.decode(r.encoding or "utf-8", errors="replace")
        raise UrlError("Zu viele Weiterleitungen")


def hint_from_url(url: str) -> dict:
    """Guess title/year from the URL slug, e.g. /movies/project-hail-mary-2026 -> ("project hail mary", 2026)."""
    segs = [
        s for s in urlparse(url).path.split("/")
        if s and not re.fullmatch(r"(tt)?\d+|[a-z]{2}(-[a-z]{2})?", s) and s.lower() not in GENERIC_SEGMENTS
    ]
    if not segs:
        return {}
    slug = re.sub(r"^\d+-", "", segs[-1])  # TMDB style "687163-project-hail-mary"
    words = [w for w in re.split(r"[-_+]", slug) if w]
    year = None
    if words and re.fullmatch(r"(19|20)\d\d", words[-1]):
        year = int(words.pop())
    if not words:
        return {}
    return {"title_from_url": " ".join(words), "year_from_url": year}


async def identify(url: str, transport: httpx.AsyncBaseTransport | None = None, check_host: bool = True) -> dict:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    elif not re.match(r"https?://", url, re.I):
        raise UrlError("Nur http- und https-Links werden unterstützt")
    result: dict = {"url": url, "ids": ids_from_text(url), **hint_from_url(url)}
    q = parse_qs(urlparse(url).query)
    if "thetvdb.com" in url and q.get("id", [""])[0].isdigit():
        result["ids"]["tvdb"] = int(q["id"][0])
    try:
        final, page = await fetch(url, transport, check_host)
        info = parse_page(page, final)
        info["ids"] = {**info["ids"], **result["ids"]}  # IDs from the URL itself win
        info["url"] = final
        hint = hint_from_url(final) or {k: v for k, v in result.items() if k.endswith("_from_url")}
        info.update(hint)
        if info["title"] and hint.get("title_from_url"):
            # App-like sites put their own name into og:title; drop a title that shares no word with the URL.
            words = {w for w in re.findall(r"\w{3,}", hint["title_from_url"].lower())}
            if words and not words & set(re.findall(r"\w{3,}", info["title"].lower())):
                info["page_title"], info["title"] = info["title"], None
        return info
    except (UrlError, httpx.HTTPError) as e:
        if result["ids"]:
            result["page_error"] = f"{e} (IDs stammen aus der Adresse)"
            return result
        raise UrlError(f"Seite konnte nicht gelesen werden: {e}") from e
