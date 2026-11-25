"""'Gibt es die aktuelle Staffel South Park schon auf Deutsch?' – per-episode report."""

import asyncio
from datetime import date

import httpx

from arrbot import german
from arrbot.arr import Sonarr
from arrbot.config import ArrConfig
from arrbot.tmdb import TMDB

TODAY = date(2026, 11, 25)


def sonarr_with_library(in_library=True):
    def handler(req):
        p, q = req.url.path.removeprefix("/api/v3"), req.url.params
        if p == "/series/lookup":
            s = {"tvdbId": 75897, "tmdbId": 2190, "title": "South Park", "year": 1997,
                 "seasons": [{"seasonNumber": n} for n in range(0, 30)]}
            if in_library:
                s["id"] = 4
            return httpx.Response(200, json=[s])
        if p == "/episode":
            assert q["includeEpisodeFile"] == "true"

            def ep(season, n, air, file_langs=None):
                e = {"seasonNumber": season, "episodeNumber": n, "title": f"Episode title {n}",
                     "airDateUtc": f"{air}T02:00:00Z", "hasFile": file_langs is not None}
                if file_langs is not None:
                    e["episodeFile"] = {"languages": [{"name": x} for x in file_langs]}
                return e

            return httpx.Response(200, json=[
                ep(27, 10, "2025-12-10", ["German"]),
                ep(28, 1, "2026-10-01", ["German", "English"]),
                ep(28, 2, "2026-10-08", ["English"]),
                ep(28, 3, "2026-10-15"),
                ep(28, 4, "2026-10-22"),
                ep(28, 5, "2026-12-03"),
            ])
        if p == "/release":
            assert q["seasonNumber"] == "28"
            return httpx.Response(200, json=[
                {"title": "South.Park.S28E02.GERMAN.DL.1080p", "episodeNumbers": [2], "languages": [{"name": "German"}]},
                {"title": "South.Park.S28E02.1080p", "episodeNumbers": [2], "languages": [{"name": "English"}]},
                {"title": "South.Park.S28E03.1080p", "episodeNumbers": [3], "languages": [{"name": "English"}]},
            ])
        return httpx.Response(404)

    return Sonarr(ArrConfig("http://s", "k"), transport=httpx.MockTransport(handler))


def tmdb_with_titles():
    def handler(req):
        p, q = req.url.path.removeprefix("/3"), req.url.params
        if p == "/tv/2190/season/28":
            de = q["language"] == "de-DE"
            names = {1: ("Die Wahl", "The Election"), 2: ("Folge 2", "Spring Break"),
                     3: ("Der Hund", "The Dog"), 4: ("Episode 4", "Taxes"), 5: ("Folge 5", "Finale")}
            return httpx.Response(200, json={"episodes": [
                {"episode_number": n, "name": names[n][0 if de else 1], "air_date": None} for n in names]})
        if p == "/tv/2190":
            return httpx.Response(200, json={"name": "South Park", "original_language": "en",
                                             "watch/providers": {"results": {"DE": {"flatrate": [
                                                 {"provider_name": "Paramount Plus"}]}}}})
        return httpx.Response(404)

    return TMDB("k", transport=httpx.MockTransport(handler))


def test_current_season_per_episode():
    text = asyncio.run(german.series_report(sonarr_with_library(), tmdb_with_titles(), 75897, None, True, TODAY))
    lines = text.split("\n")
    assert lines[0] == "🇩🇪 **South Park** – Staffel 28"  # newest season that has aired
    assert lines[1] == "Auf Deutsch bestätigt: **2 von 4** ausgestrahlten Folgen"
    assert "E01 „Die Wahl“\n   ✅ bei dir auf Deutsch" in text
    assert "E02 Episode title 2\n   ⬇️ deutscher Download da (1) – bei dir nur English" in text
    assert "E03 „Der Hund“\n   🇺🇸 nur Originalfassung gefunden" in text
    assert "E04 Episode title 4\n   ❌ noch nichts auf Deutsch" in text
    assert "E05 Episode title 5\n   ⏳ kommt erst am 03.12.2026 (Original)" in text
    assert "Streaming" not in text  # series-wide streaming info was misleading per season
    assert "25.11.2026" not in text  # the made-up "available from today" can't happen any more


def test_series_not_in_library_uses_tmdb_titles_only():
    text = asyncio.run(german.series_report(sonarr_with_library(False), tmdb_with_titles(), 75897, 28, True, TODAY))
    assert "E01 „Die Wahl“\n   🔤 deutscher Titel bekannt – deutsche Ausstrahlung wahrscheinlich" in text
    assert "E02 Spring Break\n   ❌ noch nichts auf Deutsch" in text
    assert "Nicht in deiner Bibliothek" in text and "bestätigt" not in text
