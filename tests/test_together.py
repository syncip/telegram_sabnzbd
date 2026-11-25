"""👥 Suggestions for two people (offline)."""

import asyncio

from arrbot import together
from arrbot.llm import ToolCall
from arrbot.plex import Account, Item
from arrbot.profiles import ProfileStore
from arrbot.state import Store
from arrbot.tools import ToolRunner
from arrbot.users import UserStore


class FakePlex:
    def __init__(self, libs):
        self.libs = libs

    async def library(self, a):
        return self.libs[a.user_id]


def acc(uid):
    return Account(uid, "t", f"u{uid}", "http://p", "s", "m", "Unraid", "2026")


def test_together_skips_seen_and_avoided(tmp_path):
    profiles = ProfileStore(str(tmp_path))
    profiles.learn(2, "requested", "movie", "Alien", tmdb=348, genres=["Science Fiction", "Horror"])
    profiles.learn(3, "requested", "movie", "Interstellar", tmdb=157336, genres=["Science Fiction", "Drama"])
    profiles.remember(3, avoid=["Horror"])
    users = UserStore(str(tmp_path), admin_id=1, seed_users={2, 3})
    users.touch_name(2, "Max")
    users.touch_name(3, "Anna")

    def lib(**w):  # title -> (watched by this user, genres)
        return [Item("movie", t, 2020, t, tmdb=i, genres=g, watched=seen) for i, (t, (seen, g)) in enumerate(w.items(), 1)]

    plexer = FakePlex({
        2: lib(Arrival=(False, ["Science Fiction", "Drama"]), Hereditary=(False, ["Horror"]),
               Dune=(True, ["Science Fiction"]), Paddington=(False, ["Comedy"])),
        3: lib(Arrival=(False, ["Science Fiction", "Drama"]), Hereditary=(False, ["Horror"]),
               Dune=(False, ["Science Fiction"]), Paddington=(True, ["Comedy"])),
    })
    tools = ToolRunner(None, None, Store(str(tmp_path)), "released")
    tools.profiles, tools.users, tools.plex = profiles, users, plexer
    tools.plex_store = type("S", (), {"get": staticmethod(lambda uid: acc(uid))})()

    res = asyncio.run(tools.run(ToolCall("1", "suggest_together", {"with_user": "Anna"}), 2))
    text = res.final
    assert text.startswith("👥 **Für Max und Anna**")
    assert "Arrival (2020)" in text  # sci-fi + drama: fits both
    assert "Hereditary" not in text  # Anna avoids horror
    assert "Dune" not in text and "Paddington" not in text  # one of them has seen it
    assert asyncio.run(tools.run(ToolCall("1", "suggest_together", {"with_user": "Max"}), 2)).is_error


def test_fit_prefers_titles_both_like():
    from arrbot.profiles import Prefs, Profile

    def prof(genres, avoid=()):
        return Profile(1, Prefs(avoid=list(avoid)), genres, {}, {}, None, set(), set())

    a, b = prof({"Action": 3, "Drama": 1}), prof({"Drama": 3, "Action": 0.2})
    assert together.fit(a, b, ["Drama"]) > together.fit(a, b, ["Action"])
    assert together.fit(a, prof({}, ["Horror"]), ["Horror"]) is None
