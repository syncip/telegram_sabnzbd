"""Access and rights: unapproved users get nothing, users only search/add within limits, extra rights per user."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from arrbot.llm import ToolCall
from arrbot.state import Store
from arrbot.tools import ArrError, Candidate, Flow, ToolRunner
from arrbot.users import BLOCKED, PENDING, USER, UserStore

ADMIN, SARAH, TOM, NEW = 1, 2, 3, 4


class FakeRadarr:
    kind = "Radarr"

    def __init__(self):
        self.added, self.movies = [], {10: {"id": 10, "title": "Dune", "year": 2021, "tmdbId": 438631, "hasFile": True},
                                         11: {"id": 11, "title": "Alien", "year": 1979, "tmdbId": 348, "hasFile": True}}

    async def get(self, i):
        return dict(self.movies[i])

    async def add(self, tmdb, profile, avail, search=True):
        self.added.append(tmdb)
        return {"id": 100 + len(self.added), "title": f"Film {tmdb}", "year": 2020}

    async def search(self, i):
        pass

    async def monitor(self, m):
        pass


def setup(tmp_path):
    users = UserStore(str(tmp_path), ADMIN)
    users.set_role(SARAH, USER, "Sarah")
    users.set_role(TOM, USER, "Tom")
    users.request(NEW, "Neu")
    store = Store(str(tmp_path))
    radarr = FakeRadarr()
    tools = ToolRunner(radarr, None, store, "released")
    tools.users, tools.is_admin = users, users.is_admin
    return tools, users, store, radarr


def test_unapproved_and_blocked_users_get_no_tools(tmp_path):
    tools, users, _, _ = setup(tmp_path)
    assert users.role(NEW) == PENDING
    assert tools.specs_for(NEW) == []
    res = asyncio.run(tools.run(ToolCall("1", "search_movies", {"query": "Dune"}), NEW))
    assert res.is_error
    users.set_role(TOM, BLOCKED)
    assert tools.specs_for(TOM) == [] and not tools.may(TOM, "add_movie")
    assert not users.request(NEW, "Neu")  # asked once: the admin is not asked again


def test_users_only_search_and_add(tmp_path):
    tools, _, _, _ = setup(tmp_path)
    names = {s.name for s in tools.specs_for(SARAH)}
    assert {"search_movies", "add_movie", "suggest"} <= names
    assert not names & {"remove_title", "pause_title", "german_fix", "watch_site", "build_tool"}
    assert not any(n.startswith("admin_") for n in names)
    for tool in ("remove_title", "admin_delete_media", "admin_users", "admin_permissions", "admin_set_limit"):
        res = asyncio.run(tools.run(ToolCall("1", tool, {"kind": "movie", "lib_id": 10, "who": "Sarah"}), SARAH))
        assert res.is_error, tool
    admin_names = {s.name for s in tools.specs_for(ADMIN)}
    assert {"remove_title", "admin_delete_media", "admin_permissions"} <= admin_names


def test_sarah_may_delete_only_her_own_titles(tmp_path):
    tools, users, store, _ = setup(tmp_path)
    store.record_request(SARAH, "movie", "Dune (2021)", 438631)
    res = asyncio.run(tools.run(ToolCall("1", "admin_permissions", {"who": "Sarah", "grant": ["delete_own"]}), ADMIN))
    assert "eigene Filme/Serien löschen" in res.final
    assert "remove_title" in {s.name for s in tools.specs_for(SARAH)}
    flow = asyncio.run(tools.start_remove(SARAH, "movie", 10))  # her own
    assert flow.stage == "remove"
    with pytest.raises(ArrError, match="nicht du angefragt"):
        asyncio.run(tools.start_remove(SARAH, "movie", 11))  # Alien: someone else's
    with pytest.raises(ArrError, match="nur der Admin"):
        asyncio.run(tools.start_remove(TOM, "movie", 10))  # Tom has no right
    # a button of Sarah's flow cannot be pressed in Tom's chat
    assert tools.owner_ok("flow", flow.id, SARAH, SARAH) and not tools.owner_ok("flow", flow.id, TOM, TOM)
    users.apply_perms("Sarah", revoke=["delete_own"])
    assert not tools.owner_ok("flow", flow.id, SARAH, SARAH)  # right gone: the open button is dead, too
    with pytest.raises(ValueError, match="Unbekannte Erlaubnis"):
        users.apply_perms("Sarah", grant=["alles"])


def test_title_limits_per_day(tmp_path):
    tools, users, _, radarr = setup(tmp_path)
    assert "höchstens 2 Filme pro Tag" in users.apply_limit("Sarah", "filme_tag", "2")

    def add(uid, tmdb):
        return asyncio.run(tools._add(Flow(f"f{tmdb}", uid, Candidate("movie", tmdb, f"Film {tmdb}", 2020))))

    add(SARAH, 1)
    add(SARAH, 2)
    with pytest.raises(ArrError, match="Limit ist erreicht: höchstens 2 Filme pro Tag"):
        add(SARAH, 3)
    assert radarr.added == [1, 2]
    for i in range(5):  # the admin has no limits
        add(ADMIN, 10 + i)
    users.apply_perms("Sarah", grant=["no_limits"])
    add(SARAH, 3)
    # older than a day: counts no more
    u = users.users[SARAH]
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    u.adds = [{"at": old, "filme": 5}]
    users.apply_perms("Sarah", revoke=["no_limits"])
    assert users.title_limit(SARAH, filme=1) is None


def test_episode_limit_asks_for_less():
    users = UserStore.__new__(UserStore)  # no file needed for the maths
    users.users, users.defaults, users.admin_id = {}, {"folgen_woche": 20}, ADMIN
    from arrbot.users import User

    users.users[SARAH] = User(SARAH, "Sarah", USER, "x")
    users.save = lambda: None
    users.record_adds(SARAH, folgen=15)
    msg = users.title_limit(SARAH, folgen=10)
    assert "noch 5" in msg and "nur eine Staffel" in msg
    assert users.title_limit(SARAH, folgen=5) is None


def test_total_title_limit_is_refused(tmp_path):
    _, users, _, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="pro Nutzer"):
        users.apply_limit("gesamt", "filme_tag", "5")
