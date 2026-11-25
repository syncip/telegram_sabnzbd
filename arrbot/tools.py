"""Tools exposed to the language model, and their execution."""

from __future__ import annotations

import json
import logging
from typing import Any

from .arr import ArrError, Radarr, Sonarr
from .llm import ToolCall, ToolResult, ToolSpec
from .state import Store

log = logging.getLogger(__name__)

MAX_RESULTS = 8


def _short(text: str | None, n: int = 160) -> str:
    if not text:
        return ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def build_specs(has_radarr: bool, has_sonarr: bool) -> list[ToolSpec]:
    specs: list[ToolSpec] = []
    profile = {"type": "string", "description": "Name des Qualitätsprofils. Weglassen = Standard."}
    if has_radarr:
        specs += [
            ToolSpec(
                "search_movies",
                "Sucht Filme (TMDB über Radarr). Zeigt auch, ob ein Film schon in der Bibliothek ist "
                "und ob die Datei schon vorhanden ist.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Filmtitel, gern auch englischer Originaltitel"},
                        "year": {"type": "integer", "description": "Erscheinungsjahr, falls bekannt"},
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                "add_movie",
                "Fügt einen Film zu Radarr hinzu und startet sofort die Suche nach einem Download. "
                "Nur aufrufen, wenn eindeutig klar ist, welcher Film gemeint ist.",
                {
                    "type": "object",
                    "properties": {
                        "tmdb_id": {"type": "integer"},
                        "quality_profile": profile,
                    },
                    "required": ["tmdb_id"],
                },
            ),
        ]
    if has_sonarr:
        specs += [
            ToolSpec(
                "search_series",
                "Sucht Serien (TVDB über Sonarr). Zeigt Staffeln, Status und ob die Serie schon in der "
                "Bibliothek ist.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "year": {"type": "integer", "description": "Startjahr der Serie, falls bekannt"},
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                "add_series",
                "Fügt eine Serie zu Sonarr hinzu und startet die Suche. Entweder `monitor` setzen oder "
                "mit `seasons` gezielt Staffeln wählen (dann wird `monitor` ignoriert).",
                {
                    "type": "object",
                    "properties": {
                        "tvdb_id": {"type": "integer"},
                        "monitor": {
                            "type": "string",
                            "enum": ["all", "future", "missing", "firstSeason", "pilot"],
                            "description": "all = alle Folgen, future = nur kommende, firstSeason = nur Staffel 1, "
                            "pilot = nur Pilotfolge",
                        },
                        "seasons": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Nur diese Staffelnummern laden, z. B. [3] für die neueste Staffel",
                        },
                        "quality_profile": profile,
                    },
                    "required": ["tvdb_id"],
                },
            ),
        ]
    specs += [
        ToolSpec(
            "search_existing",
            "Startet erneut eine Download-Suche für einen Film oder eine Serie, die schon in der "
            "Bibliothek ist (z. B. wenn noch keine Datei da ist oder ein Download fehlgeschlagen ist).",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["movie", "series"]},
                    "library_id": {"type": "integer", "description": "radarr_id bzw. sonarr_id aus der Suche"},
                    "seasons": {"type": "array", "items": {"type": "integer"}, "description": "Nur bei Serien"},
                },
                "required": ["kind", "library_id"],
            },
        ),
        ToolSpec(
            "list_quality_profiles",
            "Listet die verfügbaren Qualitätsprofile.",
            {
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": ["movie", "series"]}},
                "required": ["kind"],
            },
        ),
        ToolSpec(
            "download_status",
            "Zeigt die aktuelle Download-Warteschlange mit Fortschritt und Problemen.",
            {"type": "object", "properties": {}},
        ),
    ]
    return specs


class ToolRunner:
    def __init__(self, radarr: Radarr | None, sonarr: Sonarr | None, store: Store, min_availability: str):
        self.radarr = radarr
        self.sonarr = sonarr
        self.store = store
        self.min_availability = min_availability
        self.specs = build_specs(radarr is not None, sonarr is not None)

    async def run(self, call: ToolCall, chat_id: int) -> ToolResult:
        fn = getattr(self, f"_t_{call.name}", None)
        if fn is None:
            return ToolResult(call.id, call.name, f"Unbekanntes Werkzeug {call.name}", is_error=True)
        try:
            result = await fn(chat_id=chat_id, **call.args)
            return ToolResult(call.id, call.name, json.dumps(result, ensure_ascii=False))
        except ArrError as e:
            return ToolResult(call.id, call.name, str(e), is_error=True)
        except TypeError as e:
            return ToolResult(call.id, call.name, f"Ungültige Parameter: {e}", is_error=True)
        except Exception as e:  # noqa: BLE001 - report anything to the model instead of crashing
            log.exception("Tool %s fehlgeschlagen", call.name)
            return ToolResult(call.id, call.name, f"Interner Fehler: {e}", is_error=True)

    def _need(self, svc: Any, name: str) -> Any:
        if svc is None:
            raise ArrError(f"{name} ist nicht konfiguriert.")
        return svc

    # ---- tool implementations -----------------------------------------
    async def _t_search_movies(self, chat_id: int, query: str, year: int | None = None) -> list[dict]:
        radarr: Radarr = self._need(self.radarr, "Radarr")
        res = await radarr.lookup(query)
        if year:
            close = [m for m in res if m.get("year") and abs(m["year"] - year) <= 1]
            res = close or res
        return [
            {
                "tmdb_id": m.get("tmdbId"),
                "title": m.get("title"),
                "original_title": m.get("originalTitle"),
                "year": m.get("year"),
                "overview": _short(m.get("overview")),
                "in_library": bool(m.get("id")),
                "radarr_id": m.get("id") or None,
                "has_file": bool(m.get("hasFile")),
            }
            for m in res[:MAX_RESULTS]
        ]

    async def _t_search_series(self, chat_id: int, query: str, year: int | None = None) -> list[dict]:
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        res = await sonarr.lookup(query)
        if year:
            close = [s for s in res if s.get("year") and abs(s["year"] - year) <= 1]
            res = close or res
        out = []
        for s in res[:MAX_RESULTS]:
            seasons = [x["seasonNumber"] for x in s.get("seasons", []) if x["seasonNumber"] > 0]
            stats = s.get("statistics") or {}
            out.append(
                {
                    "tvdb_id": s.get("tvdbId"),
                    "title": s.get("title"),
                    "year": s.get("year"),
                    "network": s.get("network"),
                    "status": s.get("status"),
                    "seasons": seasons,
                    "overview": _short(s.get("overview")),
                    "in_library": bool(s.get("id")),
                    "sonarr_id": s.get("id") or None,
                    "episodes_downloaded": stats.get("episodeFileCount") if s.get("id") else None,
                    "episodes_monitored": stats.get("episodeCount") if s.get("id") else None,
                }
            )
        return out

    async def _t_add_movie(self, chat_id: int, tmdb_id: int, quality_profile: str | None = None) -> dict:
        radarr: Radarr = self._need(self.radarr, "Radarr")
        movie = await radarr.add(int(tmdb_id), quality_profile, self.min_availability)
        self.store.track("radarr", movie["id"], f"{movie['title']} ({movie.get('year')})", chat_id)
        return {
            "added": True,
            "title": movie["title"],
            "year": movie.get("year"),
            "note": "Suche gestartet. Der Nutzer bekommt automatisch Bescheid, wenn der Download startet und fertig ist.",
        }

    async def _t_add_series(
        self,
        chat_id: int,
        tvdb_id: int,
        monitor: str = "all",
        seasons: list[int] | None = None,
        quality_profile: str | None = None,
    ) -> dict:
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        seasons = [int(s) for s in seasons] if seasons else None
        series = await sonarr.add(int(tvdb_id), quality_profile, monitor=monitor, seasons=seasons)
        self.store.track("sonarr", series["id"], series["title"], chat_id)
        return {
            "added": True,
            "title": series["title"],
            "monitor": "Staffeln " + ", ".join(map(str, seasons)) if seasons else monitor,
            "note": "Suche gestartet. Der Nutzer bekommt automatisch Bescheid, wenn Downloads starten und fertig sind.",
        }

    async def _t_search_existing(
        self, chat_id: int, kind: str, library_id: int, seasons: list[int] | None = None
    ) -> dict:
        if kind == "movie":
            radarr: Radarr = self._need(self.radarr, "Radarr")
            movie = await radarr.get(int(library_id))
            await radarr.search(movie["id"])
            self.store.track("radarr", movie["id"], f"{movie['title']} ({movie.get('year')})", chat_id)
            return {"search_started": True, "title": movie["title"]}
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        series = await sonarr.get(int(library_id))
        await sonarr.search(series["id"], [int(s) for s in seasons] if seasons else None)
        self.store.track("sonarr", series["id"], series["title"], chat_id)
        return {"search_started": True, "title": series["title"], "seasons": seasons or "alle überwachten"}

    async def _t_list_quality_profiles(self, chat_id: int, kind: str) -> list[str]:
        svc = self._need(self.radarr if kind == "movie" else self.sonarr, "Radarr" if kind == "movie" else "Sonarr")
        return [p["name"] for p in await svc.quality_profiles()]

    async def _t_download_status(self, chat_id: int) -> list[dict]:
        out = []
        for svc in (self.radarr, self.sonarr):
            if svc is None:
                continue
            for q in await svc.queue():
                size = q.get("size") or 0
                left = q.get("sizeleft") or 0
                problems = [m for sm in q.get("statusMessages") or [] for m in sm.get("messages") or []]
                if q.get("errorMessage"):
                    problems.append(q["errorMessage"])
                out.append(
                    {
                        "title": queue_title(q),
                        "state": q.get("trackedDownloadState") or q.get("status"),
                        "health": q.get("trackedDownloadStatus"),
                        "progress_percent": round(100 * (size - left) / size) if size else None,
                        "time_left": q.get("timeleft"),
                        "problems": problems[:3],
                    }
                )
        return out or [{"info": "Die Warteschlange ist leer."}]


def queue_title(q: dict) -> str:
    if "movie" in q and q["movie"]:
        return f"{q['movie'].get('title')} ({q['movie'].get('year')})"
    if "series" in q and q["series"]:
        ep = q.get("episode") or {}
        if ep:
            return f"{q['series'].get('title')} S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
        return q["series"].get("title")
    return q.get("title", "?")
