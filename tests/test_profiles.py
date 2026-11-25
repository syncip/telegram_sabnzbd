"""User profiles: learning, stated preferences, better suggestions (all offline)."""

import asyncio

import httpx

from arrbot.agent import Agent
from arrbot.arr import Radarr
from arrbot.config import ArrConfig
from arrbot.llm import ToolCall, Turn
from arrbot.profiles import ProfileStore, render
from arrbot.state import Store
from arrbot.tmdb import TMDB
from arrbot.tools import Offer, ToolRunner, View

from .test_bot import ScriptedLLM


def radarr_lib(movies):
    def handler(req):
        if req.url.path == "/api/v3/movie":
            return httpx.Response(200, json=movies)
        return httpx.Response(404)
    return Radarr(ArrConfig("http://r", "k"), transport=httpx.MockTransport(handler))


def tmdb_recs():
    """Recommendations for Arrival: a horror film, an old one, a fitting sci-fi and a drama."""
    def handler(req):
        if req.url.path.endswith("/recommendations"):
            return httpx.Response(200, json={"results": [
                {"id": 1, "title": "Grusel", "release_date": "2019-01-01", "vote_average": 7.9, "vote_count": 9000,
                 "genre_ids": [27]},
                {"id": 2, "title": "Altfilm", "release_date": "1985-01-01", "vote_average": 7.9, "vote_count": 9000,
                 "genre_ids": [878]},
                {"id": 3, "title": "Drama", "release_date": "2020-01-01", "vote_average": 7.5, "vote_count": 9000,
                 "genre_ids": [18]},
                {"id": 4, "title": "Weltraum", "release_date": "2021-01-01", "vote_average": 7.0, "vote_count": 9000,
                 "genre_ids": [878, 12]},
            ]})
        if req.url.path.endswith("/discover/movie"):  # filling up "best of these years": nothing new
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404)
    return TMDB("k", transport=httpx.MockTransport(handler))


def test_learns_genres_and_renders(tmp_path):
    db = ProfileStore(str(tmp_path))
    db.learn(5, "requested", "movie", "Dune (2021)", tmdb=1, year=2021, genres=["Science Fiction", "Adventure"])
    db.learn(5, "requested", "movie", "Arrival (2016)", tmdb=2, year=2016, genres=["Science Fiction", "Drama"])
    db.learn(5, "requested", "movie", "Dune (2021)", tmdb=1, year=2021, genres=["Science Fiction"])  # again
    db.learn(5, "deleted", "movie", "Saw (2004)", tmdb=3, year=2004, genres=["Horror"])
    p = db.profile(5)
    assert p.genres["Science Fiction"] == 3 and p.genres["Horror"] == -1
    assert p.top_genres() == ["Science Fiction", "Abenteuer", "Drama"]
    assert p.counts == {"requested": 2, "deleted": 1}  # same title twice counts once
    assert ("movie", 1) in p.seen and p.favourite_decade() is None  # needs 3 titles
    assert p.context_line() == "[Nutzerprofil: mag Science Fiction, Abenteuer, Drama]"
    text = render(p, "Martin")
    assert "Science Fiction 60%" in text and "2 angefragt" in text and "1 gelöscht" in text
    assert db.profile(6).context_line() == ""  # someone else: nothing


def test_remember_and_forget(tmp_path):
    db = ProfileStore(str(tmp_path))
    msg = db.remember(5, avoid=["Horrorfilme", "Horror"], like=["Krimi"], from_year=2001, note="schaut mit Kindern")
    assert "keine Horror-Vorschläge mehr" in msg and "du magst Krimi" in msg and "ab 2001" in msg
    assert "Genre unbekannt: Horrorfilme" in msg
    p = db.prefs(5)
    assert p.avoid == ["Horror"] and p.like == ["Krimi"] and p.from_year == 2001 and p.notes == ["schaut mit Kindern"]
    assert db.profile(5).context_line() == "[Nutzerprofil: mag Krimi; meidet Horror; Titel ab 2001; schaut mit Kindern]"
    msg = db.remember(5, forget=["Horror", "Jahr", "Kinder"])
    p = db.prefs(5)
    assert p.avoid == [] and p.from_year is None and p.notes == [] and msg.count("vergessen") == 3
    db.forget_all(5)
    assert db.profile(5).is_empty()


def test_suggestions_use_profile(tmp_path):
    store = Store(str(tmp_path))
    store.record_request(5, "movie", "Arrival (2016)", 329865)
    tools = ToolRunner(radarr_lib([]), None, store, "released", tmdb=tmdb_recs())
    tools.profiles = db = ProfileStore(str(tmp_path))
    db.learn(5, "requested", "movie", "Arrival (2016)", tmdb=329865, year=2016, genres=["Science Fiction"])
    db.remember(5, avoid=["Horror"], from_year=2000)

    asyncio.run(tools.run(ToolCall("1", "suggest", {"kind": "movie"}), 5))
    offer = tools.take_outbox(5)[0]
    assert isinstance(offer, Offer) and offer.suggest
    # no horror, nothing before 2000 (told once), sci-fi first although rated lower than the drama
    assert [c.title for c in offer.cands] == ["Weltraum", "Drama"]
    assert "(ab 2000)" in offer.text

    assert "nicht mehr vor" in tools.reject_offer(offer.id, 5)
    asyncio.run(tools.run(ToolCall("2", "suggest", {"kind": "movie"}), 5))
    out = tools.take_outbox(5)
    assert not out or all(c.title not in ("Weltraum", "Drama") for c in out[0].cands)


def test_remember_and_profile_tools(tmp_path):
    tools = ToolRunner(None, None, Store(str(tmp_path)), "released")
    tools.profiles = ProfileStore(str(tmp_path))
    res = asyncio.run(tools.run(ToolCall("1", "remember", {"avoid": ["Horror"]}), 5))
    assert res.final == "👍 Gemerkt: keine Horror-Vorschläge mehr."
    asyncio.run(tools.run(ToolCall("2", "my_profile", {}), 5))
    assert tools.take_outbox(5) == [View(5, "profile")]


def test_agent_gets_profile_line(tmp_path):
    tools = ToolRunner(None, None, Store(str(tmp_path)), "released")
    llm = ScriptedLLM([Turn("assistant", text="Hallo!")])
    agent = Agent(llm, tools, 30, 40)
    agent.context_for = lambda uid: "[Nutzerprofil: mag Krimi]" if uid == 5 else ""
    asyncio.run(agent.handle(5, "hi", user_id=5))
    assert llm.seen[0][-1].text == "[Nutzerprofil: mag Krimi]\nhi"


def test_import_old_requests_once(tmp_path):
    store = Store(str(tmp_path))
    store.record_request(5, "movie", "Dune (2021)", 438631)
    radarr = radarr_lib([{"tmdbId": 438631, "title": "Dune", "year": 2021, "genres": ["Science Fiction"]}])
    tools = ToolRunner(radarr, None, store, "released")
    tools.profiles = ProfileStore(str(tmp_path))
    assert asyncio.run(tools.import_history()) == 1
    assert tools.profiles.profile(5).genres == {"Science Fiction": 1.0}
    assert asyncio.run(tools.import_history()) == 0  # only on the first start
