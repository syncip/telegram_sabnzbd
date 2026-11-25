"""📚 Plex collections (offline)."""

import asyncio
import json

import httpx

from arrbot import collections_plex as cp
from arrbot.plex import Account, Item, Plex

ACC = Account(1, "t", "admin", "http://p", "s", "MID", "Unraid", "2026")


def lib():
    return [Item("movie", "Iron Man", 2008, "11", tmdb=1726), Item("movie", "The Avengers", 2012, "12", tmdb=24428),
            Item("movie", "Thor", 2011, "13", tmdb=10195), Item("series", "Loki", 2021, "40", tvdb=362472)]


def test_match_is_forgiving():
    items, missing = cp.match(lib(), [{"title": "Iron Man", "year": 2008}, {"title": "Avengers"},
                                      {"title": "Thor", "year": 1999}, {"title": "Blade"}])
    assert [i.title for i in items] == ["Iron Man", "The Avengers"] and missing == ["Thor", "Blade"]


def test_put_creates_per_library_then_only_adds():
    calls = []
    existing = {}

    def handler(req):
        calls.append((req.method, req.url.path, dict(req.url.params)))
        p = req.url.path
        if p == "/library/sections":
            return httpx.Response(200, json={"MediaContainer": {"Directory": [
                {"key": 1, "type": "movie"}, {"key": 2, "type": "show"}, {"key": 3, "type": "artist"}]}})
        if p.endswith("/collections") and req.method == "GET":
            sec = p.split("/")[3]
            return httpx.Response(200, json={"MediaContainer": {"Metadata": existing.get(sec, [])}})
        if p == "/library/collections" and req.method == "POST":
            existing[req.url.params["sectionId"]] = [{"title": req.url.params["title"], "ratingKey": 900}]
            return httpx.Response(200, json={})
        if p == "/library/collections/900/children":
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [{"ratingKey": 11}]}})
        if p == "/library/collections/900/items":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    plex = Plex("c", transport=httpx.MockTransport(handler))
    coll = cp.Collections(plex)
    items = lib()
    res = asyncio.run(coll.put(ACC, "Marvel", items, ["Blade"]))
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 2 and posts[0][2]["uri"] == "server://MID/com.plexapp.plugins.library/library/metadata/11,12,13"
    assert posts[0][2]["type"] == "1" and posts[1][2]["type"] == "2" and res.created
    assert "Nicht in Plex (1): Blade" in res.text()

    calls.clear()
    res = asyncio.run(coll.put(ACC, "marvel", items[:2]))  # same name, any case: only the new one is added
    assert [c for c in calls if c[0] == "PUT"][0][2]["uri"].endswith("/metadata/12") and res.added == ["The Avengers"]


def test_wish_items():
    reqs = [{"chat_id": 2, "kind": "movie", "tmdb": 1726}, {"chat_id": 2, "kind": "series", "tvdb": 362472},
            {"chat_id": 3, "kind": "movie", "tmdb": 10195}]
    assert [i.title for i in cp.wish_items(reqs, 2, lib())] == ["Iron Man", "Loki"]
    assert json.dumps(cp.norm("Die Ärzte & Co.")) == '"arzte and co"'
