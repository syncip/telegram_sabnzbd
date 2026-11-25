"""Calendar, disk space, German watch list, weekly digest, Plex (all offline)."""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from arrbot import extras, plex, watch
from arrbot.arr import Radarr, Sonarr
from arrbot.config import ArrConfig
from arrbot.llm import ToolCall, ToolSpec, _cache_key
from arrbot.profiles import ProfileStore
from arrbot.state import Store
from arrbot.tmdb import TMDB
from arrbot.tools import Offer, ToolRunner, View

TZ = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 11, 25)  # a Wednesday


# ---- fakes -------------------------------------------------------------------------------------
class Arr:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def handler(self, req):
        p = req.url.path.removeprefix("/api/v3")
        self.calls.append((req.method, p, dict(req.url.params), json.loads(req.content) if req.content else None))
        for (method, path), resp in self.routes.items():
            if req.method == method and p == path:
                return httpx.Response(200, json=resp(req) if callable(resp) else resp)
        return httpx.Response(404)


def radarr(routes):
    fake = Arr(routes)
    return fake, Radarr(ArrConfig("http://r", "k"), transport=httpx.MockTransport(fake.handler))


def sonarr(routes):
    fake = Arr(routes)
    return fake, Sonarr(ArrConfig("http://s", "k"), transport=httpx.MockTransport(fake.handler))


def tmdb(digital=None, streaming=None):
    def handler(req):
        if req.url.path == "/3/movie/603":
            rd = []
            if digital:
                rd.append({"type": 4, "release_date": f"{digital}T00:00:00.000Z"})
            return httpx.Response(200, json={
                "title": "Matrix", "original_language": "en",
                "release_dates": {"results": [{"iso_3166_1": "DE", "release_dates": rd}]},
                "watch/providers": {"results": {"DE": {"flatrate": [{"provider_name": p} for p in streaming or []]}}},
            })
        return httpx.Response(404)
    return TMDB("k", transport=httpx.MockTransport(handler))


# ---- calendar ------------------------------------------------------------------------------------
def test_calendar_groups_days_and_filters_mine():
    _, s = sonarr({("GET", "/calendar"): [
        {"seasonNumber": 2, "episodeNumber": 3, "title": "Neue Folge", "airDateUtc": "2026-11-25T20:00:00Z",
         "hasFile": False, "series": {"title": "Dark", "tvdbId": 1}},
        {"seasonNumber": 1, "episodeNumber": 1, "title": "TBA", "airDateUtc": "2026-11-26T23:30:00Z",
         "hasFile": True, "series": {"title": "Andere", "tvdbId": 2}},
    ]})
    _, r = radarr({("GET", "/calendar"): [
        {"title": "Dune 3", "year": 2026, "tmdbId": 9, "digitalRelease": "2026-11-28T00:00:00Z", "hasFile": False},
    ]})
    week = TODAY + timedelta(days=6)
    entries = asyncio.run(extras.calendar_entries(r, s, TODAY, week, TZ))
    text = extras.render_calendar(entries, TODAY, week, TODAY, False)
    assert "<b>Heute 25.11.</b>\n📺 Dark S02E03 „Neue Folge“ · 21:00" in text
    assert "<b>Fr 27.11.</b>\n📺 Andere S01E01 · 00:30 ✅" in text  # 23:30 UTC is Friday in Berlin, TBA dropped
    assert "<b>Sa 28.11.</b>\n🎬 Dune 3 (2026) – digital" in text
    mine = asyncio.run(extras.calendar_entries(r, s, TODAY, week, TZ, {("series", 1)}))
    assert [e.text.split(" ·")[0] for e in mine] == ["📺 Dark S02E03 „Neue Folge“"]


def test_disk_space_and_warning():
    _, r = radarr({("GET", "/rootfolder"): [{"path": "/media/movies"}],
                   ("GET", "/diskspace"): [{"path": "/", "freeSpace": 9e9, "totalSpace": 1e11},
                                            {"path": "/media", "freeSpace": 30 * 2**30, "totalSpace": 4 * 2**40}]})
    _, s = sonarr({("GET", "/rootfolder"): [{"path": "/media/tv"}],
                   ("GET", "/diskspace"): [{"path": "/media", "freeSpace": 30 * 2**30, "totalSpace": 4 * 2**40}]})
    ds = asyncio.run(extras.disks(r, s))
    assert len(ds) == 1 and ds[0].path == "/media/movies"  # same drive only once
    text = extras.render_disks(ds, 50, ["• Dune (2021) · 45 GB"])
    assert "⚠️" in text and "30 GB frei von 4,0 TB" in text and "99 % belegt" in text and "Dune (2021)" in text
    assert extras.low_disks(ds, 50) == ds and extras.low_disks(ds, 10) == [] and extras.low_disks(ds, 0) == []


# ---- German watch list -------------------------------------------------------------------------------
def runner(tmp_path, r=None, s=None, t=None):
    tools = ToolRunner(r, s, Store(str(tmp_path)), "released", tmdb=t)
    tools.profiles = ProfileStore(str(tmp_path))
    tools.is_admin = lambda uid: True
    return tools


def test_movie_watch_starts_search_when_german_is_out(tmp_path):
    fake, r = radarr({("GET", "/movie/lookup/tmdb"): {"tmdbId": 603, "title": "The Matrix", "year": 1999, "id": 7},
                      ("GET", "/movie/7"): {"id": 7, "hasFile": False},
                      ("POST", "/command"): {"id": 1}})
    tools = runner(tmp_path, r=r, t=tmdb(digital="2026-12-20"))
    res = asyncio.run(tools.run(ToolCall("1", "notify_german", {"kind": "movie", "id": 603}), 5))
    assert res.final.startswith("🔔 Mach ich!") and len(tools.profiles.watches(5)) == 1

    tools.tmdb = tmdb(digital="2026-11-20")  # German digital release has arrived
    notices = asyncio.run(watch.check_watches(tools, TODAY))
    assert "gibt es jetzt auf Deutsch (digital seit 20.11.2026)" in notices[0].text
    assert any(c[1] == "/command" and c[3]["name"] == "MoviesSearch" for c in fake.calls)
    assert tools.profiles.watches(5) == [] and "radarr:7" in tools.store.state.tracked


def test_movie_watch_not_in_library_offers_to_add(tmp_path):
    _, r = radarr({("GET", "/movie/lookup/tmdb"): {"tmdbId": 603, "title": "Matrix", "year": 1999}})
    tools = runner(tmp_path, r=r, t=tmdb(streaming=["Netflix"]))
    tools.profiles.add_watch(5, "movie", 603, "Matrix (1999)")
    notices = asyncio.run(watch.check_watches(tools, TODAY))
    assert notices[0].outbox
    offer = tools.take_outbox(5)[0]
    assert isinstance(offer, Offer) and "im Stream bei Netflix" in offer.text


def test_series_watch_reports_only_new_german_episodes(tmp_path):
    eps = [{"id": 100 + n, "seasonNumber": 1, "episodeNumber": n, "airDateUtc": f"2026-10-0{n}T20:00:00Z",
            "hasFile": False} for n in (1, 2)]
    releases = [{"title": "Show.S01E01.GERMAN", "guid": "g1", "indexerId": 1, "episodeNumbers": [1],
                 "languages": [{"name": "German"}]}]
    fake, s = sonarr({("GET", "/series/lookup"): [{"tvdbId": 50, "title": "Show", "id": 4, "status": "continuing"}],
                      ("GET", "/series/4"): {"id": 4, "title": "Show"},
                      ("GET", "/episode"): eps, ("GET", "/release"): lambda req: releases})
    tools = runner(tmp_path, s=s)
    tools.profiles.add_watch(5, "series", 50, "Show")
    n = asyncio.run(watch.check_watches(tools, TODAY))
    assert n and n[0].outbox
    flow = tools.take_outbox(5)[0]
    assert flow.stage == "gfix" and flow.step.text.startswith("🔔 Neue deutsche Folgen von Show!")
    assert asyncio.run(watch.check_watches(tools, TODAY)) == []  # nothing new since yesterday
    releases.append({"title": "Show.S01E02.GERMAN", "guid": "g2", "indexerId": 1, "episodeNumbers": [2],
                     "languages": [{"name": "German"}]})
    assert asyncio.run(watch.check_watches(tools, TODAY))  # E02 is new


def test_weekly_digest(tmp_path):
    _, s = sonarr({("GET", "/calendar"): [
        {"seasonNumber": 1, "episodeNumber": 5, "airDateUtc": "2026-11-27T20:00:00Z",
         "series": {"title": "Dark", "tvdbId": 1}}]})
    tools = runner(tmp_path, s=s)
    tools.store.record_request(5, "series", "Dark", 70523, 1)
    tools.store.state.finished.append({"title": "Dune <2>", "chat_ids": [5], "at": "2026-11-24T10:00:00+00:00"})
    res = asyncio.run(tools.run(ToolCall("1", "weekly_digest", {"day": "Sonntag"}), 5))
    assert "jeden Sonntag" in res.final and tools.profiles.digest_users(6) == [5]
    d = asyncio.run(watch.weekly_digest(tools, 5, TZ, TODAY))
    assert "• Dune &lt;2&gt;" in d.text and "Fr 27.11. 📺 Dark S01E05" in d.text
    asyncio.run(tools.run(ToolCall("2", "weekly_digest", {"off": True}), 5))
    assert tools.profiles.digest_users(6) == []
    assert watch.parse_day("mittwoch") == 2 and watch.parse_day("Mo") == 0 and watch.parse_day("x") is None


# ---- Plex ------------------------------------------------------------------------------------------
def plex_fake(calls):
    def handler(req):
        calls.append(str(req.url))
        p = req.url.path
        if req.url.host == "plex.tv":
            if p == "/api/v2/pins" and req.method == "POST":
                return httpx.Response(201, json={"id": 77, "code": "ABCD"})
            if p == "/api/v2/pins/77":
                return httpx.Response(200, json={"authToken": "acct"})
            if p == "/api/v2/user":
                return httpx.Response(200, json={"username": "martin"})
            if p == "/api/v2/resources":
                return httpx.Response(200, json=[
                    {"name": "Fremd", "provides": "server", "clientIdentifier": "other", "owned": False,
                     "accessToken": "t2", "connections": [{"uri": "http://other:32400", "local": False}]},
                    {"name": "Unraid", "provides": "server", "clientIdentifier": "mid1", "owned": True,
                     "accessToken": "srv", "connections": [{"uri": "http://10.0.0.5:32400", "local": True}]},
                    {"name": "Handy", "provides": "player", "clientIdentifier": "p"},
                ])
        assert req.headers["X-Plex-Token"] in ("srv", "t2")
        if p == "/identity":
            mid = "mid1" if req.url.host == "10.0.0.5" else "other"
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": mid}})
        if p == "/library/sections":
            return httpx.Response(200, json={"MediaContainer": {"Directory": [
                {"key": "1", "type": "movie"}, {"key": "2", "type": "show"}, {"key": "3", "type": "artist"}]}})
        if p == "/library/sections/1/all":
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [
                {"type": "movie", "title": "Dune", "year": 2021, "ratingKey": 11, "viewCount": 2,
                 "Guid": [{"id": "tmdb://438631"}], "Genre": [{"tag": "Science Fiction"}]},
                {"type": "movie", "title": "Ungesehen", "year": 2020, "ratingKey": 12, "Guid": [{"id": "tmdb://1"}]},
            ]}})
        if p == "/library/sections/2/all":
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [
                {"type": "show", "title": "Dark", "year": 2017, "ratingKey": 20, "viewedLeafCount": 3,
                 "Guid": [{"id": "tvdb://334824"}, {"id": "tmdb://70523"}], "Genre": [{"tag": "Drama"}]}]}})
        if p == "/hubs/search":
            return httpx.Response(200, json={"MediaContainer": {"Hub": [{"Metadata": [
                {"type": "show", "title": "Dark", "year": 2017, "ratingKey": 20}]}]}})
        if p == "/library/metadata/20":
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [{"title": "Dark"}]}})
        if p == "/library/metadata/20/allLeaves":
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [
                {"parentIndex": 1, "index": 1, "viewCount": 1, "ratingKey": 201},
                {"parentIndex": 1, "index": 2, "viewCount": 1, "ratingKey": 202},
                {"parentIndex": 1, "index": 3, "title": "Vergangenheit", "viewOffset": 600000,
                 "duration": 3000000, "ratingKey": 203},
                {"parentIndex": 1, "index": 4, "ratingKey": 204},
            ]}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_plex_link_pick_server_sync_and_progress(tmp_path):
    calls = []
    store = plex.PlexStore(str(tmp_path))
    client = plex.Plex(store.client_id(), transport=plex_fake(calls))
    assert store.client_id() == client.client_id  # stable

    async def link():
        pin_id, code = await client.create_pin()
        token = await plex.wait_for_pin(client, pin_id, every=0.01, minutes=1)
        url, stoken, mid, name = await client.pick_server(token)
        return code, token, url, stoken, mid, name, await client.username(token)

    code, token, url, stoken, mid, name, user = asyncio.run(link())
    assert (code, token, url, stoken, mid, name, user) == (
        "ABCD", "acct", "http://10.0.0.5:32400", "srv", "mid1", "Unraid", "martin")  # owned server first
    acc = plex.Account(5, token, user, url, stoken, mid, name, plex.utcnow())
    store.save(acc)
    assert store.get(5).server_name == "Unraid"

    profiles = ProfileStore(str(tmp_path))
    assert asyncio.run(plex.sync(client, store, profiles, acc)) == 2  # Dune + Dark, not the unwatched one
    assert asyncio.run(plex.sync(client, store, profiles, acc)) == 0  # nothing new: weights don't grow twice
    p = profiles.profile(5)
    assert p.genres == {"Science Fiction": 1.5, "Drama": 1.5} and p.counts["watched"] == 2
    assert store.get(5).synced

    tools = ToolRunner(None, None, Store(str(tmp_path)), "released")
    tools.plex, tools.plex_store = client, store
    asyncio.run(tools.run(ToolCall("1", "plex_status", {"title": "Dark"}), 5))
    view = tools.take_outbox(5)[0]
    assert view.kind == "plex"
    assert view.arg["text"] == ("📺 Dark (2017): 2 von 4 Folgen gesehen\n"
                                "▶️ Weiter mit S01E03 „Vergangenheit“ – bei 10 von 50 min")
    assert view.arg["url"].endswith("server/mid1/details?key=%2Flibrary%2Fmetadata%2F203")

    asyncio.run(tools.run(ToolCall("2", "plex_status", {}), 6))  # not linked: offer to link
    assert tools.take_outbox(6) == [View(6, "plex_link")]


def test_cache_key_per_tool_set():
    a = [ToolSpec("x", "", {}), ToolSpec("y", "", {})]
    assert _cache_key(a) == _cache_key(list(a)) != _cache_key(a + [ToolSpec("admin_stats", "", {})])
    assert datetime.now(timezone.utc)  # keep import used
