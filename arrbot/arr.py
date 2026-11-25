"""Thin async clients for the Radarr and Sonarr v3 APIs."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from .config import ArrConfig

log = logging.getLogger(__name__)


class ArrError(Exception):
    pass


class _ArrBase:
    kind = "arr"

    def __init__(self, cfg: ArrConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=f"{cfg.url}/api/v3",
            headers={"X-Api-Key": cfg.api_key},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )
        self._profiles: list[dict] | None = None

    async def close(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ArrError(f"{self.kind} nicht erreichbar: {e}") from e
        if r.status_code >= 400:
            detail = r.text[:500]
            try:
                body = r.json()
                if isinstance(body, list):
                    detail = "; ".join(str(x.get("errorMessage", x)) for x in body)
                elif isinstance(body, dict):
                    detail = str(body.get("message") or body)
            except ValueError:
                pass
            raise ArrError(f"{self.kind} {method} {path} -> HTTP {r.status_code}: {detail}")
        if not r.content:
            return None
        return r.json()

    # ---- shared helpers -------------------------------------------------
    async def quality_profiles(self) -> list[dict]:
        if self._profiles is None:
            self._profiles = await self._req("GET", "/qualityprofile")
        return self._profiles

    async def resolve_profile(self, name: str | None) -> dict:
        profiles = await self.quality_profiles()
        if not profiles:
            raise ArrError(f"In {self.kind} sind keine Qualitätsprofile angelegt.")
        wanted = name or self.cfg.quality_profile
        if wanted:
            for p in profiles:
                if p["name"].lower() == wanted.lower():
                    return p
            names = ", ".join(p["name"] for p in profiles)
            raise ArrError(f"Qualitätsprofil {wanted!r} gibt es nicht. Verfügbar: {names}")
        return profiles[0]

    async def root_folder(self) -> str:
        if self.cfg.root_folder:
            return self.cfg.root_folder
        folders = await self._req("GET", "/rootfolder")
        if not folders:
            raise ArrError(f"In {self.kind} ist kein Root-Ordner angelegt.")
        return folders[0]["path"]

    async def history_since(self, since: datetime) -> list[dict]:
        params = {"date": since.strftime("%Y-%m-%dT%H:%M:%SZ"), **self._history_params}
        return await self._req("GET", "/history/since", params=params) or []

    async def queue(self) -> list[dict]:
        data = await self._req("GET", "/queue", params={"pageSize": 500, **self._queue_params})
        return (data or {}).get("records", [])

    async def releases(self, **params: Any) -> list[dict]:
        """Interactive search on the indexers (slow, costs indexer API hits: use sparingly)."""
        return await self._req("GET", "/release", params=params, timeout=90.0) or []

    _item_path = ""  # "/movie" or "/series"

    async def set_monitored(self, item_id: int, monitored: bool) -> dict:
        """Off = Radarr/Sonarr stop searching for it (pause); on = search again."""
        item = await self._req("GET", f"{self._item_path}/{item_id}")
        item["monitored"] = monitored
        return await self._req("PUT", f"{self._item_path}/{item_id}", json=item)

    async def delete(self, item_id: int, delete_files: bool) -> None:
        """Remove from the library; running downloads of it are removed from the download client too."""
        id_field = "movieId" if self._item_path == "/movie" else "seriesId"
        for q in await self.queue():
            if q.get(id_field) == item_id and q.get("id"):
                try:
                    await self._req("DELETE", f"/queue/{q['id']}",
                                    params={"removeFromClient": "true", "blocklist": "false"})
                except ArrError as e:
                    log.warning("Warteschlangen-Eintrag %s nicht entfernt: %s", q["id"], e)
        params = {"deleteFiles": str(delete_files).lower()}
        if self._item_path == "/movie":
            params["addImportExclusion"] = "false"
        await self._req("DELETE", f"{self._item_path}/{item_id}", params=params)
        log.info("%s: %s %s gelöscht (Dateien: %s)", self.kind, self._item_path, item_id, delete_files)

    async def command(self, name: str, **body: Any) -> dict:
        return await self._req("POST", "/command", json={"name": name, **body})

    _history_params: dict = {}
    _queue_params: dict = {}


class Radarr(_ArrBase):
    kind = "Radarr"
    _item_path = "/movie"
    _history_params = {"includeMovie": "true"}
    _queue_params = {"includeMovie": "true"}

    async def lookup(self, term: str) -> list[dict]:
        return await self._req("GET", "/movie/lookup", params={"term": term}) or []

    async def lookup_tmdb(self, tmdb_id: int) -> dict:
        return await self._req("GET", "/movie/lookup/tmdb", params={"tmdbId": tmdb_id})

    async def get(self, movie_id: int) -> dict:
        return await self._req("GET", f"/movie/{movie_id}")

    async def library(self) -> list[dict]:
        return await self._req("GET", "/movie") or []

    async def add(self, tmdb_id: int, profile: str | None, min_availability: str, search: bool = True) -> dict:
        movie = await self.lookup_tmdb(tmdb_id)
        if movie.get("id"):
            raise ArrError(f"'{movie.get('title')}' ist schon in Radarr (id {movie['id']}).")
        prof = await self.resolve_profile(profile)
        movie.update(
            qualityProfileId=prof["id"],
            rootFolderPath=await self.root_folder(),
            monitored=True,
            minimumAvailability=min_availability,
            # No search-on-add: that one is an automatic search and skips movies Radarr does not yet
            # consider "available". The explicit command below counts as manual, like the Search button.
            addOptions={"searchForMovie": False},
        )
        created = await self._req("POST", "/movie", json=movie)
        if search:
            await self.search(created["id"])
        return created

    async def search(self, movie_id: int) -> dict:
        cmd = await self.command("MoviesSearch", movieIds=[movie_id])
        log.info("Radarr: Suche für Film %s gestartet (Befehl %s)", movie_id, (cmd or {}).get("id"))
        return cmd


class Sonarr(_ArrBase):
    kind = "Sonarr"
    _item_path = "/series"
    _history_params = {"includeSeries": "true", "includeEpisode": "true"}
    _queue_params = {"includeSeries": "true", "includeEpisode": "true"}

    async def lookup(self, term: str) -> list[dict]:
        return await self._req("GET", "/series/lookup", params={"term": term}) or []

    async def lookup_tvdb(self, tvdb_id: int) -> dict:
        res = await self.lookup(f"tvdb:{tvdb_id}")
        if not res:
            raise ArrError(f"Keine Serie mit TVDB-ID {tvdb_id} gefunden.")
        return res[0]

    async def get(self, series_id: int) -> dict:
        return await self._req("GET", f"/series/{series_id}")

    async def library(self) -> list[dict]:
        return await self._req("GET", "/series") or []

    async def grab(self, release: dict) -> None:
        """Download one release from an interactive search (like the download button in Sonarr)."""
        await self._req("POST", "/release", json={"guid": release["guid"], "indexerId": release["indexerId"]})
        log.info("Sonarr: Release geholt: %s", release.get("title"))

    async def delete_episode_files(self, file_ids: list[int]) -> None:
        if not file_ids:
            return
        try:
            await self._req("DELETE", "/episodefile/bulk", json={"episodeFileIds": file_ids})
        except ArrError:
            for fid in file_ids:  # older Sonarr without the bulk endpoint
                await self._req("DELETE", f"/episodefile/{fid}")
        log.info("Sonarr: %d Folgen-Dateien gelöscht", len(file_ids))

    async def set_episodes_monitored(self, episode_ids: list[int], monitored: bool) -> None:
        if episode_ids:
            await self._req("PUT", "/episode/monitor", json={"episodeIds": episode_ids, "monitored": monitored})

    async def search_episodes(self, episode_ids: list[int]) -> None:
        if episode_ids:
            await self.command("EpisodeSearch", episodeIds=episode_ids)

    async def episodes(self, series_id: int, season: int | None = None) -> list[dict]:
        """Episodes incl. their file (with audio languages) if downloaded."""
        params: dict[str, Any] = {"seriesId": series_id, "includeEpisodeFile": "true"}
        if season is not None:
            params["seasonNumber"] = season
        return await self._req("GET", "/episode", params=params) or []

    async def _language_profile_id(self) -> int | None:
        # Only Sonarr v3 has language profiles; v4 removed them.
        try:
            profiles = await self._req("GET", "/languageprofile")
        except ArrError:
            return None
        return profiles[0]["id"] if profiles else None

    async def add(
        self,
        tvdb_id: int,
        profile: str | None,
        monitor: str = "all",
        seasons: list[int] | None = None,
        search: bool = True,
    ) -> dict:
        series = await self.lookup_tvdb(tvdb_id)
        if series.get("id"):
            raise ArrError(f"'{series.get('title')}' ist schon in Sonarr (id {series['id']}).")
        prof = await self.resolve_profile(profile)
        series.update(
            qualityProfileId=prof["id"],
            rootFolderPath=await self.root_folder(),
            monitored=True,
            seasonFolder=True,
        )
        lang = await self._language_profile_id()
        if lang is not None:
            series["languageProfileId"] = lang

        if seasons:
            # Add without monitoring, then switch on only the requested seasons.
            series["addOptions"] = {"monitor": "none", "searchForMissingEpisodes": False}
            for s in series.get("seasons", []):
                s["monitored"] = False
            created = await self._req("POST", "/series", json=series)
            created = await self.set_seasons_monitored(created["id"], seasons)
            if search:
                for n in seasons:
                    await self.command("SeasonSearch", seriesId=created["id"], seasonNumber=n)
            return created

        series["addOptions"] = {"monitor": monitor, "searchForMissingEpisodes": search}
        return await self._req("POST", "/series", json=series)

    async def set_seasons_monitored(self, series_id: int, seasons: list[int]) -> dict:
        series = await self.get(series_id)
        for s in series.get("seasons", []):
            if s["seasonNumber"] in seasons:
                s["monitored"] = True
        return await self._req("PUT", f"/series/{series_id}", json=series)

    async def search(self, series_id: int, seasons: list[int] | None = None) -> None:
        if seasons:
            await self.set_seasons_monitored(series_id, seasons)
            for n in seasons:
                await self.command("SeasonSearch", seriesId=series_id, seasonNumber=n)
        else:
            await self.command("SeriesSearch", seriesId=series_id)
