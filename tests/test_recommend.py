"""Better suggestions (1.13): moods, variety, 👍/👎 per title, ratings, quick start, Plex stars (all offline)."""

import asyncio

import httpx

from arrbot import plex, recommend
from arrbot.arr import Radarr
from arrbot.config import ArrConfig
from arrbot.llm import ToolCall
from arrbot.profiles import ProfileStore
from arrbot.state import Store
from arrbot.tmdb import TMDB, Recommendation
from arrbot.tools import Offer, ToolRunner


def rec(i, title, genres, rating=7.5, votes=5000, year=2020):
    return Recommendation("movie", i, title, year, rating, votes, genres=genres)


def test_moods_and_genres():
    assert recommend.wanted_genres("was Lustiges und Spannendes", None) >= {"Komödie", "Thriller"}
    assert recommend.wanted_genres(None, ["krimi", "Horror"]) == {"Krimi", "Horror"}
    assert recommend.wanted_genres("irgendwas", None) == set()
    assert 35 in recommend.genre_ids("movie", {"Komödie"})


def test_diversify_one_per_film_series_and_a_hidden_gem():
    ranked = [rec(1, "Harry Potter und der Stein der Weisen", ["Fantasy"]),
              rec(2, "Harry Potter und die Kammer des Schreckens", ["Fantasy"]),
              rec(3, "Der Hobbit", ["Fantasy"]), rec(4, "Herr der Ringe", ["Fantasy"]),
              rec(5, "Krimi A", ["Krimi"]), rec(6, "Perle", ["Drama"], rating=7.8, votes=400)]
    out = recommend.diversify(ranked, 4)
    titles = [r.title for r in out]
    assert "Harry Potter und die Kammer des Schreckens" not in titles  # same franchise
    assert "Perle" in titles and "💎 Geheimtipp" in out[-1].because
    assert sum(1 for r in out if r.genres == ["Fantasy"]) <= 2  # genre cap
    assert recommend.reason(out[-1]) == "💎 Geheimtipp"


def test_seeds_prefer_liked_and_skip_disliked(tmp_path):
    db = ProfileStore(str(tmp_path))
    db.learn(5, "requested", "movie", "Arrival (2016)", tmdb=1)
    db.learn(5, "watched", "movie", "Dune (2021)", tmdb=2)
    db.learn(5, "liked", "movie", "Alien (1979)", tmdb=3)
    db.learn(5, "watched", "movie", "Cats (2019)", tmdb=4)
    db.learn(5, "disliked", "movie", "Cats (2019)", tmdb=4)
    seeds = db.seeds(5)
    assert [s[2] for s in seeds] == ["Alien", "Dune", "Arrival"]
    db.learn(5, "liked", "movie", "Cats (2019)", tmdb=4)  # changed their mind: 👍 replaces 👎
    assert (("movie", 4) not in db.profile(5).rejected) and "Cats" in [s[2] for s in db.seeds(5)]


def tmdb_fake(calls=None):
    def handler(req):
        p = req.url.path
        if calls is not None:
            calls.append((p, dict(req.url.params)))
        if p.endswith("/recommendations"):
            return httpx.Response(200, json={"results": [
                {"id": 11, "title": "Ernst", "release_date": "2019-01-01", "vote_average": 8.0, "vote_count": 9000,
                 "genre_ids": [18]},
                {"id": 12, "title": "Lachen", "release_date": "2020-01-01", "vote_average": 7.0, "vote_count": 9000,
                 "genre_ids": [35]},
                {"id": 13, "title": "Lang und lustig", "release_date": "2021-01-01", "vote_average": 7.2,
                 "vote_count": 9000, "genre_ids": [35]},
            ]})
        if p == "/3/movie/13" or p == "/movie/13":
            return httpx.Response(200, json={"runtime": 170})
        if p.endswith("/movie/12"):
            return httpx.Response(200, json={"runtime": 85})
        if p.endswith("/discover/movie"):
            return httpx.Response(200, json={"results": [
                {"id": 21, "title": "Komödie neu", "release_date": "2022-01-01", "vote_average": 7.5,
                 "vote_count": 900, "genre_ids": [35]}]})
        if p.endswith("/trending/movie/week"):
            return httpx.Response(200, json={"results": [
                {"id": 30 + i, "title": f"Bekannt {i}", "release_date": "2020-01-01", "vote_average": 7,
                 "vote_count": 1000 * i, "genre_ids": [g]} for i, g in enumerate([28, 35, 18, 27, 878], 1)]})
        return httpx.Response(404)
    return TMDB("k", transport=httpx.MockTransport(handler))


def radarr_fake(movies, lookup=()):
    def handler(req):
        if req.url.path == "/api/v3/movie":
            return httpx.Response(200, json=movies)
        if req.url.path == "/api/v3/movie/lookup":
            return httpx.Response(200, json=list(lookup))
        return httpx.Response(404)
    return Radarr(ArrConfig("http://r", "k"), transport=httpx.MockTransport(handler))


def runner(tmp_path, movies=(), lookup=(), calls=None):
    tools = ToolRunner(radarr_fake(list(movies), lookup), None, Store(str(tmp_path)), "released",
                       tmdb=tmdb_fake(calls))
    tools.profiles = ProfileStore(str(tmp_path))
    return tools


def test_mood_runtime_and_server_tip(tmp_path):
    tools = runner(tmp_path, movies=[{"id": 1, "tmdbId": 11, "title": "Ernst", "hasFile": True}])
    tools.profiles.learn(5, "liked", "movie", "Arrival (2016)", tmdb=329865, genres=["Science Fiction"])
    res = asyncio.run(tools.run(ToolCall("1", "suggest", {"mood": "was Lustiges", "max_minutes": 90}), 5))
    assert res.final == ""
    offer = tools.take_outbox(5)[0]
    # "Lang und lustig" runs 170 minutes, "Ernst" is no comedy; too few: filled up from the genre search
    assert [c.title for c in offer.cands] == ["Lachen", "Komödie neu"]
    assert offer.text.startswith("🎯 Passend zu deinem Wunsch (was Lustiges bis 90 Min.):")

    res = asyncio.run(tools.run(ToolCall("2", "suggest", {}), 5))
    offer = tools.take_outbox(5)[0]
    assert "📼 Schon auf dem Server: Ernst (2019) – wie Arrival" in offer.text  # owned, not seen by this user


def test_like_x_and_unknown_title(tmp_path):
    tools = runner(tmp_path, lookup=[{"tmdbId": 329865, "title": "Arrival", "year": 2016}])
    asyncio.run(tools.run(ToolCall("1", "suggest", {"like": "Arrival"}), 5))
    offer = tools.take_outbox(5)[0]
    assert offer.text.startswith("💡 Wie Arrival:")
    tools2 = runner(tmp_path / "b")
    res = asyncio.run(tools2.run(ToolCall("1", "suggest", {"like": "Gibtsnicht"}), 5))
    assert "nicht gefunden" in res.final and "?" in res.final  # asks back


def test_quick_start_then_feedback(tmp_path):
    tools = runner(tmp_path)
    asyncio.run(tools.run(ToolCall("1", "suggest", {}), 5))
    offer = tools.take_outbox(5)[0]
    assert isinstance(offer, Offer) and offer.calibrate and len(offer.cands) == 5
    assert offer.cands[0].title == "Bekannt 5"  # best known first
    o, text, ask = tools.offer_feedback(offer.id, 0, "l", 5)
    assert text == "👍 Bekannt 5" and ask is None and o.hidden == [0]
    tools.offer_feedback(offer.id, 1, "d", 5)
    seeds = tools.profiles.seeds(5)
    assert [s[2] for s in seeds] == ["Bekannt 5"]
    assert ("movie", 34) in tools.profiles.profile(5).rejected

    asyncio.run(tools.run(ToolCall("2", "suggest", {}), 5))  # no second quick start, real suggestions now
    offer = tools.take_outbox(5)[0]
    assert not offer.calibrate and "weil du Bekannt 5 magst" in offer.text
    _, text, ask = tools.offer_feedback(offer.id, 0, "s", 5)
    assert ask and tools.rate(ask, 5, 5).startswith("👍")
    assert tools.rate(ask, 5, 5) is None  # answered once
    assert "nicht mehr vor" in tools.reject_offer(offer.id, 5)


def test_title_feedback_tool(tmp_path):
    tools = runner(tmp_path, lookup=[{"tmdbId": 99, "title": "Inception", "year": 2010, "genres": ["Action"]}])
    res = asyncio.run(tools.run(ToolCall("1", "title_feedback", {"title": "Inception", "opinion": "liked"}), 5))
    assert res.final.startswith("👍 Gemerkt: Inception")
    assert tools.profiles.seeds(5)[0][:3] == ("movie", 99, "Inception")


class FakePlex:
    def __init__(self, items):
        self.items = items

    async def library(self, a):
        return self.items


class FakePlexStore:
    def mark_synced(self, uid):
        pass


def test_plex_stars_abandoned_and_fresh(tmp_path):
    now = 1_800_000_000
    items = [
        plex.Item("movie", "Toll", 2020, "1", tmdb=1, watched=True, last_viewed=now - 3600, user_rating=10.0),
        plex.Item("movie", "Mies", 2020, "2", tmdb=2, watched=True, last_viewed=now - 3600, user_rating=2.0),
        plex.Item("movie", "Abgebrochen", 2020, "3", tmdb=3, started=True, last_viewed=now - 90 * 86400),
        plex.Item("movie", "Frisch", 2020, "4", tmdb=4, watched=True, last_viewed=now - 3600),
        plex.Item("movie", "Alt", 2020, "5", tmdb=5, watched=True, last_viewed=now - 30 * 86400),
    ]
    db = ProfileStore(str(tmp_path))
    fresh = []
    acc = plex.Account(5, "t", "u", "http://p", "s", "m", "n", "x")
    assert asyncio.run(plex.sync(FakePlex(items), FakePlexStore(), db, acc, fresh, now=now)) == 4
    assert [i.title for i in fresh] == ["Frisch"]  # rated ones and old ones are not asked
    p = db.profile(5)
    assert ("movie", 2) in p.rejected and ("movie", 3) in p.rejected
    assert db.seeds(5)[0][2] == "Toll"


def test_situation_cardio_short_and_light(tmp_path):
    """'Serien für Cardio auf dem Rad': light genres, short runtime – the taste (drama) must not win."""
    assert recommend.situation_of("Cardio auf dem Rad im Fitnessstudio").key == "nebenbei"
    assert recommend.situation_of(None, "was zum Einschlafen").key == "einschlafen"
    assert recommend.situation_of("was Spannendes") is None
    s = recommend.SITUATIONS["nebenbei"]
    assert s.fits(["Komödie", "Drama"]) and not s.fits(["Action", "Drama"]) and not s.fits(["Komödie", "Horror"])

    tools = runner(tmp_path, movies=[{"id": 1, "tmdbId": 11, "title": "Ernst", "hasFile": True}])
    tools.profiles.learn(5, "liked", "movie", "Arrival (2016)", tmdb=329865, genres=["Science Fiction"])
    asyncio.run(tools.run(ToolCall("1", "suggest", {"kind": "movie", "situation": "beim Cardio auf dem Rad",
                                                   "genres": ["Science Fiction"]}), 5))
    offer = tools.take_outbox(5)[0]
    titles = [c.title for c in offer.cands]
    assert "Ernst" not in titles and "Lang und lustig" not in titles  # drama / 170 min out
    assert "Lachen" in titles and "Komödie neu" in titles
    assert offer.text.startswith("🎯 Für nebenbei") and "85 Min." in offer.text
    assert "Schon auf dem Server: Ernst" not in offer.text  # a drama is no cardio tip either
