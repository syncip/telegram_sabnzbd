"""🛠 Tools the bot builds for itself (offline: fake model, fake TMDB)."""

import asyncio

import httpx
import pytest

from arrbot import recipes
from arrbot.agent import UsageStats
from arrbot.llm import ToolCall, Turn
from arrbot.tmdb import TMDB
from arrbot.tools import Offer, ToolRunner
from arrbot.users import UserStore

from .test_features import make

MARVEL = {
    "name": "studio_top",
    "title": "Top-Filme eines Studios",
    "description": "Meistgesehene Filme eines Studios (z. B. Marvel, Pixar).",
    "params": {"studio": {"type": "string", "description": "Studio"},
               "count": {"type": "integer", "description": "Anzahl"}},
    "steps": [{"source": "tmdb", "path": "/search/company", "query": {"query": "{studio}"},
               "pick": "results.0.id", "as": "company"}],
    "source": "tmdb", "path": "/discover/movie",
    "query": {"with_companies": "{company}", "sort_by": "vote_count.desc", "language": "de-DE",
              "primary_release_year": "{year}"},
    "items": "results",
    "filters": [{"field": "vote_average", "op": "gte", "value": 6}],
    "heading": "🏆 Top {count} {studio}-Filme",
    "line": "{title} ({release_date|year}) ⭐ {vote_average|1}",
    "offer": "movie", "id_field": "id", "limit": 10,
}


def fake_tmdb(calls):
    def handler(req):
        p, q = req.url.path.removeprefix("/3"), dict(req.url.params)
        calls.append((p, q))
        if p == "/search/company":
            return httpx.Response(200, json={"results": [{"id": 420, "name": "Marvel Studios"}]})
        if p == "/discover/movie":
            return httpx.Response(200, json={"results": [
                {"id": 299534, "title": "Avengers: Endgame", "release_date": "2019-04-24", "vote_average": 8.25},
                {"id": 1, "title": "Schlechter Film", "release_date": "2020-01-01", "vote_average": 4.0},
                {"id": 24428, "title": "Marvel's The Avengers", "release_date": "2012-04-25", "vote_average": 7.7},
                {"id": 693134, "title": "Schon da", "release_date": "2024-02-27", "vote_average": 8.0},
            ]})
        return httpx.Response(404)

    return TMDB("abc", transport=httpx.MockTransport(handler))


class FakeBuilder:
    def __init__(self, answers):
        self.answers, self.seen = list(answers), []

    async def complete(self, system, history, tools):
        self.seen.append(history[-1].text)
        recipe = self.answers.pop(0)
        return Turn("assistant", tool_calls=[ToolCall("b", "save_recipe", {
            "recipe": recipe, "example_args": {"studio": "Marvel", "count": 3}})],
            usage={"input": 2000, "cached": 0, "output": 600})


def test_only_reading_endpoints():
    recipes.validate(MARVEL)
    for source, path in (("radarr", "/command"), ("sonarr", "/series/editor"), ("tmdb", "/account/1/rated"),
                         ("radarr", "/movie/1/../../command"), ("plex", "/library")):
        with pytest.raises(recipes.RecipeError):
            recipes.validate({**MARVEL, "source": source, "path": path})
    with pytest.raises(recipes.RecipeError, match="name"):
        recipes.validate({**MARVEL, "name": "Böse Name"})


def test_run_steps_filter_template():
    calls = []
    out = asyncio.run(recipes.run(recipes.validate(MARVEL), {"studio": "Marvel", "count": 2},
                                  {"tmdb": fake_tmdb(calls)}))
    assert out.heading == "🏆 Top 2 Marvel-Filme"
    assert out.lines == ["Avengers: Endgame (2019) ⭐ 8,2", "Marvel's The Avengers (2012) ⭐ 7,7"]
    disc = [q for p, q in calls if p == "/discover/movie"][0]
    assert disc["with_companies"] == "420" and "primary_release_year" not in disc  # unset optional param dropped


def test_build_keep_and_use_only_for_admins(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    fr.movies[1] = {"id": 1, "tmdbId": 693134, "title": "Schon da", "hasFile": True}
    calls = []
    tools = ToolRunner(radarr, sonarr, store, "released", tmdb=fake_tmdb(calls))
    tools.users = UserStore(str(tmp_path), admin_id=1, seed_users={5})
    tools.is_admin = tools.users.is_admin
    tools.recipes = recipes.RecipeStore(str(tmp_path))
    broken = {**MARVEL, "path": "/command", "source": "radarr"}
    tools.builder = FakeBuilder([broken, MARVEL])  # first try fails the check, the second is fixed
    tools.builder_model = "gpt-6.1-terra"
    tools.builder_cost = lambda u: UsageStats.cost("gpt-6.1-terra", u.get("input", 0), u.get("cached", 0), u.get("output", 0))
    booked = []
    tools.on_llm_usage = lambda uid, u, cost: booked.append((uid, u["input"] + u["output"], cost))

    res = asyncio.run(tools.run(ToolCall("1", "build_tool", {"request": "Top 3 Marvel-Filme"}), 5))
    assert res.is_error and "nur der Admin" in res.content  # users never build

    asyncio.run(tools.run(ToolCall("1", "build_tool", {"request": "Top 3 Marvel-Filme"}), 1))
    [flow] = tools.take_outbox(1)
    assert "kein Werkzeug" in flow.step.text and "ct" in flow.step.buttons[0][0]
    flow = asyncio.run(tools.choose(flow.id, "build"))
    assert "nicht erlaubt" in tools.builder.seen[1]  # the error went back to the model
    assert "Probelauf" in flow.step.text and "Avengers: Endgame (2019) ⭐ 8,2" in flow.step.text
    assert booked == [(1, 5200, pytest.approx(0.01))] and "5.200 Tokens ≈ 1,00 ct" in flow.step.text
    flow = asyncio.run(tools.choose(flow.id, "keep"))
    assert flow.step.done and "gespeichert" in flow.step.text

    assert "recipe_studio_top" in {s.name for s in tools.specs_for(1)}
    assert "recipe_studio_top" not in {s.name for s in tools.specs_for(5)}
    assert asyncio.run(tools.run(ToolCall("2", "recipe_studio_top", {"studio": "Marvel"}), 5)).is_error

    res = asyncio.run(tools.run(ToolCall("2", "recipe_studio_top", {"studio": "Marvel", "count": 3}), 1))
    offer = tools.take_outbox(1)[0]
    assert isinstance(offer, Offer) and offer.text.startswith("🏆 Top 3 Marvel-Filme\n1. Avengers: Endgame")
    assert [c.title for c in offer.cands] == ["Avengers: Endgame", "Marvel's The Avengers"]  # owned one left out
    assert tools.recipes.get("studio_top").uses == 1
    assert "gelöscht" in asyncio.run(tools.run(ToolCall("3", "admin_recipes",
                                                        {"action": "delete", "name": "studio_top"}), 1)).final


def test_filled_in_path_is_checked_again():
    r = recipes.validate({**MARVEL, "steps": [], "source": "radarr", "path": "/movie/{id}", "query": {},
                          "params": {"id": {"type": "string"}}, "items": ""})
    with pytest.raises(recipes.RecipeError, match="nicht erlaubt"):
        asyncio.run(recipes.run(r, {"id": "1/../../command"}, {"radarr": object()}))
