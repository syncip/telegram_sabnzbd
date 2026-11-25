"""Thin async clients for the Radarr and Sonarr v3 APIs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from . import debuglog
from .config import ArrConfig

log = logging.getLogger(__name__)


class ArrError(Exception):
    pass


_WATCHERS: set[asyncio.Task] = set()


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
        started = asyncio.get_running_loop().time()
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            debuglog.LOG.add("🌐", f"{self.kind} {method} {path} → nicht erreichbar: {e}")
            raise ArrError(f"{self.kind} nicht erreichbar: {e}") from e
        if debuglog.CURRENT.get() is not None:
            took = asyncio.get_running_loop().time() - started
            extra = ""
            if method != "GET" and kw.get("json") is not None:
                extra = " " + debuglog.short(kw["json"], 160)
            elif kw.get("params"):
                extra = " " + debuglog.short(kw["params"], 120)
            debuglog.LOG.add("🌐", f"{self.kind} {method} {path}{extra} → {r.status_code} ({took:.1f}s)"
                             + (f": {debuglog.short(r.text, 200)}" if r.status_code >= 400 else ""))
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

    async def root_folders(self) -> list[dict]:
        return await self._req("GET", "/rootfolder") or []

    async def diskspace(self) -> list[dict]:
        return await self._req("GET", "/diskspace") or []

    async def calendar(self, start: Any, end: Any) -> list[dict]:
        """Episodes (Sonarr, with series) or movies (Radarr) with a date between start and end."""
        params = {"start": start.isoformat(), "end": end.isoformat(), "unmonitored": "false"}
        if self._item_path == "/series":
            params["includeSeries"] = "true"
        return await self._req("GET", "/calendar", params=params) or []

    COMMAND_WAIT = 300.0  # seconds: how long the bot waits for a search command to finish
    COMMAND_EVERY = 4.0
    # Set by the bot: called when a search started while answering a chat is done
    # (chat_id, command name, command body, final command state).
    on_search_done: Any = None

    async def command(self, name: str, **body: Any) -> dict:
        cmd = await self._req("POST", "/command", json={"name": name, **body})
        chat = debuglog.CURRENT.get()
        if chat is not None and isinstance(cmd, dict) and cmd.get("id") and "Search" in name:
            task = asyncio.create_task(self._watch_command(chat, name, cmd["id"], body))
            _WATCHERS.add(task)
            task.add_done_callback(_WATCHERS.discard)
        return cmd

    async def _watch_command(self, chat: int, name: str, cmd_id: int, body: dict | None = None) -> None:
        """Wait until Sonarr/Radarr report the search done ("… 0 reports downloaded"): log it and let the
        bot check why nothing was loaded."""
        debuglog.CURRENT.set(None)  # the polling itself is not interesting
        loop = asyncio.get_running_loop()
        end = loop.time() + self.COMMAND_WAIT
        state: dict = {}
        try:
            while loop.time() < end:
                await asyncio.sleep(self.COMMAND_EVERY)
                state = await self._req("GET", f"/command/{cmd_id}") or {}
                if state.get("status") in ("completed", "failed", "aborted", "cancelled", "orphaned"):
                    break
            else:
                state = {**state, "message": f"nach {self.COMMAND_WAIT:.0f} s noch nicht fertig "
                                             f"({state.get('status', '?')})"}
        except ArrError as e:
            state = {"status": "?", "message": str(e)}
        msg = state.get("message") or state.get("exception") or ""
        icon = "✅" if state.get("status") == "completed" else "⚠️"
        text = f"{self.kind} {name}: {state.get('status', '?')}" + (f" – {msg}" if msg else "")
        debuglog.LOG.late(chat, icon, text)
        if debuglog.LOG.notify and chat in debuglog.LOG.live:
            try:
                await debuglog.LOG.notify(chat, f"{icon} {text}")
            except Exception:  # noqa: BLE001
                log.exception("Debug-Nachricht nicht gesendet")
        if self.on_search_done:
            try:
                await self.on_search_done(chat, name, body or {}, state)
            except Exception:  # noqa: BLE001
                log.exception("%s: Auswertung der Suche fehlgeschlagen", self.kind)

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

    async def grab(self, release: dict) -> None:
        await self._req("POST", "/release", json={"guid": release["guid"], "indexerId": release["indexerId"]})
        log.info("Radarr: Release geholt: %s", release.get("title"))

    async def delete_movie_file(self, file_id: int) -> None:
        await self._req("DELETE", f"/moviefile/{file_id}")
        log.info("Radarr: Filmdatei %s gelöscht", file_id)

    async def monitor(self, movie: dict) -> None:
        """Radarr only grabs for monitored movies (RSS, later searches) – switch it on before searching."""
        if not movie.get("monitored", True):
            movie["monitored"] = True
            await self._req("PUT", f"/movie/{movie['id']}", json=movie)

    async def search(self, movie_id: int) -> dict:
        cmd = await self.command("MoviesSearch", movieIds=[movie_id])
        log.info("Radarr: Suche für Film %s gestartet (Befehl %s)", movie_id, (cmd or {}).get("id"))
        return cmd


class Sonarr(_ArrBase):
    kind = "Sonarr"
    _item_path = "/series"
    READY_TRIES, READY_EVERY = 45, 2.0  # wait up to 90 s for Sonarr to load the episode list of a new series

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._tasks: set[asyncio.Task] = set()

    async def wait_pending(self) -> None:
        """Tests: wait for background work after adding a series."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _monitor_and_search(self, series_id: int, seasons: list[int], search: bool) -> None:
        """Monitor the episodes of these seasons and search them.

        Monitoring a season alone is not enough: Sonarr only searches *monitored episodes*. A new series
        gets its episode list in a refresh after adding, and at the end of that refresh Sonarr applies the
        monitoring chosen when adding ("none") – which would undo ours. So wait until the refresh is done."""
        eps: list[dict] = []
        for _ in range(self.READY_TRIES):
            eps = await self.episodes(series_id)
            if eps and not await self._refreshing(series_id):
                break
            await asyncio.sleep(self.READY_EVERY)
        await self.set_seasons_monitored(series_id, seasons)
        ids = [e["id"] for e in eps if e.get("seasonNumber") in seasons]
        if not ids:
            log.warning("Sonarr: Serie %s hat noch keine Folgen für Staffel %s", series_id, seasons)
        await self.set_episodes_monitored(ids, True)
        if search:
            for n in seasons:
                await self.command("SeasonSearch", seriesId=series_id, seasonNumber=n)
        log.info("Sonarr: Serie %s – %d Folgen überwacht, Suche %s", series_id, len(ids), "gestartet" if search else "aus")

    async def _refreshing(self, series_id: int) -> bool:
        """Is Sonarr still refreshing this series (loading episodes, applying monitoring)?"""
        try:
            cmds = await self._req("GET", "/command") or []
        except ArrError:
            return False
        return any(c.get("name") == "RefreshSeries" and c.get("status") in ("queued", "started")
                   and (c.get("body") or {}).get("seriesId") in (series_id, None) for c in cmds)

    def _background(self, coro: Any) -> None:
        async def run() -> None:
            try:
                await coro
            except Exception:  # noqa: BLE001
                log.exception("Sonarr: Staffeln überwachen/suchen fehlgeschlagen")

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
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

    async def episode_files(self, series_id: int) -> list[dict]:
        """All files of a series, with the real audio languages (mediaInfo)."""
        return await self._req("GET", "/episodefile", params={"seriesId": series_id}) or []

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
            # Only the requested seasons. Their episodes are monitored and searched as soon as Sonarr
            # has loaded them (in the background, the answer doesn't wait for it).
            series["addOptions"] = {"monitor": "none", "searchForMissingEpisodes": False}
            for s in series.get("seasons", []):
                s["monitored"] = s.get("seasonNumber") in seasons
            created = await self._req("POST", "/series", json=series)
            self._background(self._monitor_and_search(created["id"], seasons, search))
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
            await self._monitor_and_search(series_id, seasons, True)
        else:
            await self.command("SeriesSearch", seriesId=series_id)

    async def search_missing(self, series_id: int, seasons: list[int] | None = None,
                             now: str | None = None) -> tuple[list[dict], bool]:
        """Search the aired episodes without a file again ("such nochmal").

        Sonarr's own SeriesSearch skips unmonitored series and episodes without a word, so the missing
        episodes (and their series/seasons) are monitored first and then searched by id.
        Returns (searched episodes, series was unmonitored)."""
        from datetime import datetime, timezone

        now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        eps = await self.episodes(series_id)
        missing = [e for e in eps if not e.get("hasFile") and not e.get("episodeFileId")
                   and (e.get("seasonNumber") in seasons if seasons else e.get("seasonNumber", 0) > 0)
                   and (e.get("airDateUtc") or "") <= now and e.get("airDateUtc")]
        series = await self.get(series_id)
        was_off = not series.get("monitored", True)
        # Named seasons are monitored even if nothing has aired yet (Sonarr loads them when they come).
        want = set(seasons) if seasons else {e["seasonNumber"] for e in missing}
        if want and (was_off or any(s["seasonNumber"] in want and not s.get("monitored")
                                    for s in series.get("seasons", []))):
            series["monitored"] = True
            for s in series.get("seasons", []):
                if s["seasonNumber"] in want:
                    s["monitored"] = True
            await self._req("PUT", f"/series/{series_id}", json=series)
        watch = [e for e in eps if e.get("seasonNumber") in want and not e.get("hasFile")] if seasons else missing
        await self.set_episodes_monitored([e["id"] for e in watch if not e.get("monitored")], True)
        # A season with every aired episode missing: season search, so Sonarr takes a season pack if there
        # is one. Partly missing seasons: only those episodes.
        aired = [e for e in eps if e.get("airDateUtc") and e["airDateUtc"] <= now]
        whole = sorted(n for n in want
                       if len([e for e in missing if e["seasonNumber"] == n])
                       == len([e for e in aired if e.get("seasonNumber") == n]))
        for n in whole:
            await self.command("SeasonSearch", seriesId=series_id, seasonNumber=n)
        await self.search_episodes([e["id"] for e in missing if e["seasonNumber"] not in whole])
        log.info("Sonarr: Serie %s – %d fehlende Folgen neu gesucht (ganze Staffeln: %s)", series_id,
                 len(missing), whole or "-")
        return missing, was_off
