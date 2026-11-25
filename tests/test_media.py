"""Media check (admin), real audio languages, adding single seasons (all offline)."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from arrbot import german, manage, media
from arrbot.arr import Sonarr
from arrbot.config import ArrConfig
from arrbot.plex import Account, Item

from .test_extras import radarr, sonarr


# ---- real audio languages ---------------------------------------------------------------------
def f(audio=None, named=None, scene=None):
    out = {"languages": [{"name": n} for n in named or []]}
    if audio is not None:
        out["mediaInfo"] = {"audioLanguages": audio}
    if scene:
        out["sceneName"] = scene
    return out


def test_audio_track_beats_release_name():
    assert german.audio_langs(f("eng/ger")) == ["English", "German"]
    assert german.audio_langs(f("English / German")) == ["English", "German"]
    assert german.audio_langs(f("und")) is None and german.audio_langs(f()) is None
    assert german.file_check(f("eng", ["German"])) == "mislabelled"  # the "Eine schrecklich nette Familie" case
    assert german.file_check(f("eng", ["English"])) == "other"
    assert german.file_check(f("ger/eng", ["English"])) == "german"  # name wrong, audio fine
    assert german.file_check(f(None, ["German"])) == "german"  # not analysed: trust the name
    assert german.file_check(f()) == "unknown"
    assert german.langs_de(["English", "Klingon"]) == "Englisch, Klingon"


def test_german_fix_replaces_mislabelled_episode_with_other_release():
    eps = [
        {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "airDateUtc": "1987-04-05T20:00:00Z", "hasFile": True,
         "episodeFileId": 11, "episodeFile": f("ger", ["German"], "MWC.S01E01.GERMAN.DL")},
        {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "airDateUtc": "1987-04-12T20:00:00Z", "hasFile": True,
         "episodeFileId": 12, "episodeFile": f("eng", ["German"], "MWC.S01E02.GERMAN.BAD")},
    ]
    releases = [  # the release that turned out English must not come back
        {"title": "MWC.S01E02.GERMAN.BAD", "guid": "bad", "indexerId": 1, "episodeNumbers": [2],
         "approved": True, "seeders": 99, "languages": [{"name": "German"}]},
        {"title": "MWC.S01E02.GERMAN.GOOD", "guid": "good", "indexerId": 1, "episodeNumbers": [2],
         "languages": [{"name": "German"}]},
    ]
    fake, s = sonarr({("GET", "/series/4"): {"id": 4, "title": "Eine schrecklich nette Familie"},
                      ("GET", "/episode"): eps, ("GET", "/release"): releases,
                      ("POST", "/release"): {}, ("DELETE", "/episodefile/bulk"): {}, ("PUT", "/episode/monitor"): {}})
    plan = asyncio.run(manage.plan_german(s, 4, 1, datetime(2026, 11, 25).date()))
    assert [e["id"] for e in plan.german] == [1] and [e["id"] for e in plan.mislabelled] == [2]
    assert [g.release["guid"] for g in plan.grabs] == ["good"]
    text, buttons = manage.render_plan(plan)
    assert "🏷 Davon 1 als Deutsch benannt, die Tonspur ist aber Englisch (S01E02)" in text
    asyncio.run(manage.run_fix(s, plan, only_missing=False))
    assert [c[3] for c in fake.calls if c[:2] == ("POST", "/release")] == [{"guid": "good", "indexerId": 1}]
    assert [c[3] for c in fake.calls if c[1] == "/episodefile/bulk"] == [{"episodeFileIds": [12]}]


def test_movie_fix_replaces_file():
    movie = {"id": 7, "title": "Dune", "year": 2021, "hasFile": True,
             "movieFile": {"id": 70, **f("eng", ["German"], "Dune.2021.GERMAN.BAD")}}
    fake, r = radarr({("GET", "/movie/7"): movie, ("GET", "/release"): [
        {"title": "Dune.2021.GERMAN.BAD", "guid": "bad", "indexerId": 2, "languages": [{"name": "German"}]},
        {"title": "Dune.2021.German.DL.1080p", "guid": "ok", "indexerId": 2, "size": 8 * 2**30,
         "languages": [{"name": "German"}]}],
        ("POST", "/release"): {}, ("DELETE", "/moviefile/70"): {}})
    fix = asyncio.run(manage.plan_movie(r, 7))
    text, buttons = manage.render_movie(fix)
    assert text.startswith("🏷 Dune (2021): Die Datei heißt Deutsch, die Tonspur ist aber Englisch.")
    assert "Dune.2021.German.DL.1080p (8,0 GB)" in text and buttons[0] == ("🔁 Datei ersetzen", "all")
    asyncio.run(manage.run_movie(r, fix))
    assert ("DELETE", "/moviefile/70") in [c[:2] for c in fake.calls]
    assert [c[3] for c in fake.calls if c[:2] == ("POST", "/release")] == [{"guid": "ok", "indexerId": 2}]


# ---- media check --------------------------------------------------------------------------------
class FakePlex:
    def __init__(self, libs):
        self.libs = libs

    async def library(self, a):
        return self.libs[a.user_id]


def acc(uid):
    return Account(uid, "t", f"user{uid}", "http://p", "s", "m", "Unraid", "2026-01-01")


def test_media_check_sections():
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    _, r = radarr({("GET", "/movie"): [
        {"id": 1, "title": "Gesehen", "year": 2020, "tmdbId": 101, "hasFile": True, "sizeOnDisk": 40 * 2**30,
         "added": old, "monitored": True, "movieFile": f("ger")},
        {"id": 2, "title": "Halb", "year": 2020, "tmdbId": 102, "hasFile": True, "sizeOnDisk": 5 * 2**30,
         "added": old, "monitored": True, "movieFile": f("ger")},
        {"id": 3, "title": "Staub", "year": 2010, "tmdbId": 103, "hasFile": True, "sizeOnDisk": 20 * 2**30,
         "added": old, "monitored": True, "movieFile": f("eng", ["German"])},
        {"id": 4, "title": "Fehlt", "year": 2025, "tmdbId": 104, "hasFile": False, "monitored": True, "isAvailable": True},
        {"id": 5, "title": "Neu", "year": 2026, "tmdbId": 105, "hasFile": True, "sizeOnDisk": 2**30, "added": new,
         "monitored": True},
        {"id": 6, "title": "Nicht in Plex", "year": 2024, "tmdbId": 106, "hasFile": True, "sizeOnDisk": 2**30,
         "added": new, "monitored": True},
    ]})
    _, s = sonarr({("GET", "/series"): [
        {"id": 9, "title": "MWC", "year": 1987, "tvdbId": 900, "status": "ended", "monitored": True, "added": old,
         "statistics": {"episodeFileCount": 3, "episodeCount": 4, "sizeOnDisk": 3 * 2**30}}],
        ("GET", "/episodefile"): [{"id": 1, **f("ger")}, {"id": 2, **f("eng", ["German"])}, {"id": 3, **f("eng")}]})

    def lib(**watched):
        return [Item("movie", n, 2020, str(i), tmdb=i, watched=w) for n, (i, w) in watched.items()] + [
            Item("series", "MWC", 1987, "9", tvdb=900, watched=True, leaves=3, viewed_leaves=1)]

    plexer = FakePlex({1: lib(Gesehen=(101, True), Halb=(102, True), Staub=(103, False), Neu=(105, False)),
                       2: lib(Gesehen=(101, True), Halb=(102, False), Staub=(103, False), Neu=(105, False))})
    rep = asyncio.run(media.collect(r, s, plexer, [acc(1), acc(2)]))
    names = {k: [t.title for t in rep.section(k, 180)] for k in media.SECTIONS}
    assert names["missing"] == ["Fehlt", "MWC"]  # MWC: 1 of 4 episodes missing
    assert names["not_in_plex"] == ["Nicht in Plex"]
    assert names["watched"] == ["Gesehen"]  # "Halb": only one of the two has seen it
    assert names["unwatched"] == ["Staub"]  # old and nobody started it; "Neu" is too new
    assert names["not_german"] == ["MWC", "Staub"]  # more wrong files first
    mwc = next(t for t in rep.titles if t.title == "MWC")
    assert (mwc.not_german, mwc.mislabelled) == (2, 1)

    text, buttons = media.render_overview(rep, 180, lambda u: f"user{u}")
    assert "💤 Lange da, nie geschaut (seit über 180 Tagen): <b>1</b> · 20,0 GB" in text
    text, buttons = media.render_section(rep, "watched", 180, str)
    assert ("🗑 Gesehen (2020) · 40,0 GB", "n:rm:radarr:1") in buttons
    text, buttons = media.render_section(rep, "not_german", 180, str)
    assert "MWC (1987) – 2 von 3 Folgen ohne deutsche Tonspur, 1 davon heißen Deutsch" in text
    assert ("🇩🇪 MWC (1987)", "m:gf:s:900") in buttons


# ---- adding single seasons ---------------------------------------------------------------------
def test_season_monitoring_waits_for_sonarr_refresh():
    state = {"polls": 0, "monitored": None, "seasons": None}

    def handler(req):
        p = req.url.path.removeprefix("/api/v3")
        if p == "/episode":
            state["polls"] += 1
            return httpx.Response(200, json=[{"id": 5, "seasonNumber": 1}, {"id": 6, "seasonNumber": 2}])
        if p == "/command" and req.method == "GET":
            running = state["polls"] < 3  # refresh still busy for the first two looks
            return httpx.Response(200, json=[{"name": "RefreshSeries", "status": "started" if running else "completed",
                                              "body": {"seriesId": 4}}])
        if p == "/series/4" and req.method == "GET":
            return httpx.Response(200, json={"id": 4, "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}]})
        if p == "/series/4" and req.method == "PUT":
            import json
            state["seasons"] = {x["seasonNumber"]: x.get("monitored") for x in json.loads(req.content)["seasons"]}
            return httpx.Response(202, json={})
        if p == "/episode/monitor":
            import json
            state["monitored"] = json.loads(req.content)["episodeIds"]
            return httpx.Response(202, json={})
        if p == "/command":
            return httpx.Response(201, json={})
        return httpx.Response(404)

    s = Sonarr(ArrConfig("http://s", "k"), transport=httpx.MockTransport(handler))
    s.READY_EVERY = 0.01
    asyncio.run(s._monitor_and_search(4, [1], True))
    assert state["polls"] == 3  # waited for the refresh before touching monitoring
    assert state["seasons"] == {1: True, 2: None} and state["monitored"] == [5]


