"""📌 Wish list for unreleased titles (offline)."""

import asyncio
from datetime import date

from arrbot import wishlist
from arrbot.llm import ToolCall
from arrbot.state import Store
from arrbot.tools import ToolRunner

from .test_extras import radarr

TODAY = date(2026, 11, 25)


def setup(tmp_path, lookup):
    fake, r = radarr({
        ("GET", "/movie/lookup/tmdb"): lambda req: lookup(),
        ("GET", "/qualityprofile"): [{"id": 1, "name": "HD"}], ("GET", "/rootfolder"): [{"path": "/movies"}],
        ("POST", "/movie"): {"id": 3, "title": "Avatar 4", "year": 2029}, ("POST", "/command"): {"id": 1},
    })
    tools = ToolRunner(r, None, Store(str(tmp_path)), "released")
    tools.wishes = wishlist.WishStore(str(tmp_path))
    return fake, tools


def test_no_date_yet_is_kept_then_added_when_a_date_appears(tmp_path):
    item = {"title": "Avatar 4", "year": 2029}
    fake, tools = setup(tmp_path, lambda: item)
    res = asyncio.run(tools.run(ToolCall("1", "wishlist", {"action": "add", "kind": "movie", "id": 393209}), 2))
    assert res.final.startswith("📌 Avatar 4 (2029) ist gemerkt. Noch gibt es keinen Termin")
    assert not [c for c in fake.calls if c[:2] == ("POST", "/movie")]
    assert asyncio.run(wishlist.check(tools, tools.wishes, TODAY)) == []  # still no date: nothing happens

    item["inCinemas"] = "2029-12-21T00:00:00Z"
    out = asyncio.run(wishlist.check(tools, tools.wishes, TODAY))
    assert out == [(2, "📅 Avatar 4 (2029) hat jetzt einen Termin (Kino 21.12.2029). Ich habe es hinzugefügt "
                       "und melde mich zum Download.")]
    assert [c for c in fake.calls if c[:2] == ("POST", "/movie")] and tools.wishes.wishes() == []


def test_with_date_is_added_at_once(tmp_path):
    fake, tools = setup(tmp_path, lambda: {"title": "Dune 3", "year": 2026, "inCinemas": "2026-12-17"})
    res = asyncio.run(tools.run(ToolCall("1", "wishlist", {"action": "add", "kind": "movie", "id": 1}), 2))
    assert "hat schon einen Termin (Kino 17.12.2026). Ich habe es angelegt" in res.final
    assert wishlist.release_date("movie", {"inCinemas": "2030-01-01", "digitalRelease": "2029-05-01"}) == (
        date(2029, 5, 1), "digital")
    assert "leer" in wishlist.render([])
