"""Approval rules: hold wishes until the admin agrees (offline)."""

import asyncio

from arrbot import approval
from arrbot.state import Store
from arrbot.tools import Candidate, Flow, Held, ToolRunner
from arrbot.users import UserStore

from .test_extras import radarr

GB = 2**30


def setup(tmp_path, releases):
    movie = {"title": "Dune", "year": 2021, "tmdbId": 438631, "genres": ["Science Fiction", "Adventure"]}
    horror = {"title": "Smile", "year": 2022, "tmdbId": 882598, "genres": ["Horror"]}
    routes = {
        ("GET", "/movie/lookup/tmdb"): lambda req: horror if req.url.params["tmdbId"] == "882598" else movie,
        ("GET", "/qualityprofile"): [{"id": 1, "name": "HD"}], ("GET", "/rootfolder"): [{"path": "/movies"}],
        ("POST", "/movie"): lambda req: {"id": 7, "title": "Dune", "year": 2021},
        ("GET", "/release"): releases, ("POST", "/command"): {"id": 1}, ("POST", "/release"): {},
        ("GET", "/movie/7"): {"id": 7, "monitored": True}, ("PUT", "/movie/7"): {"id": 7},
        ("GET", "/queue"): {"records": []}, ("DELETE", "/movie/7"): {},
    }
    fake, r = radarr(routes)
    users = UserStore(str(tmp_path), admin_id=1, seed_users={2, 3})
    users.touch_name(2, "Anna")
    users.touch_name(3, "Ben")
    tools = ToolRunner(r, None, Store(str(tmp_path)), "released")
    tools.users, tools.is_admin = users, users.is_admin
    tools.approvals = approval.ApprovalStore(str(tmp_path))
    asked, told = [], []

    async def on_approval(p):
        asked.append(p)

    async def notify(uid, text):
        told.append((uid, text))

    tools.on_approval, tools.notify_user = on_approval, notify
    return fake, tools, asked, told


def add(tools, uid, tmdb_id=438631, title="Dune"):
    return asyncio.run(tools._add(Flow("f", uid, Candidate("movie", tmdb_id, title, 2021))))


def test_rule_for_one_user_holds_before_adding(tmp_path):
    fake, tools, asked, told = setup(tmp_path, [])
    tools.approvals.add(2)
    try:
        add(tools, 2)
        raise AssertionError("not held")
    except Held:
        pass
    assert not [c for c in fake.calls if c[:2] == ("POST", "/movie")]  # nothing added yet
    assert asked[0].title.startswith("Dune") and asked[0].user_id == 2
    add(tools, 3)  # Ben has no rule
    assert len([c for c in fake.calls if c[:2] == ("POST", "/movie")]) == 1

    p, text = asyncio.run(tools.decide(asked[0].id, True))
    assert text.startswith("✅ Freigegeben") and len([c for c in fake.calls if c[:2] == ("POST", "/movie")]) == 2
    assert told == [(2, "✅ Dune (2021): freigegeben und hinzugefügt.")]
    assert asyncio.run(tools.decide(asked[0].id, True)) == (None, "Schon erledigt.")


def test_genre_rule_only_holds_horror(tmp_path):
    fake, tools, asked, told = setup(tmp_path, [])
    rule = tools.approvals.add(2, "movie", ["Horror"])
    assert rule.text(lambda u: "Anna") == "Filme von Anna, wenn Genre Horror"
    add(tools, 2)  # Dune: no horror
    try:
        add(tools, 2, 882598, "Smile")
        raise AssertionError("not held")
    except Held:
        pass
    assert asked[0].reason == "Genre Horror"


def test_size_rule_holds_big_download_unmonitored(tmp_path):
    big = [{"guid": "g1", "indexerId": 1, "size": 58 * GB, "title": "Dune.2160p", "customFormatScore": 10},
           {"guid": "g2", "indexerId": 1, "size": 9 * GB, "title": "Dune.1080p", "rejected": True}]
    fake, tools, asked, told = setup(tmp_path, big)
    tools.approvals.add(None, "all", min_gb=20)
    try:
        add(tools, 2)
        raise AssertionError("not held")
    except Held:
        pass
    p = asked[0]
    assert p.arr_id == 7 and p.release["guid"] == "g1" and p.reason == "Download 58,0 GB (Grenze 20 GB)"
    assert fake.calls[-1][1] == "/movie/7" and fake.calls[-1][3]["monitored"] is False
    assert not [c for c in fake.calls if c[:2] == ("POST", "/command")]  # no search started

    asyncio.run(tools.decide(p.id, True))
    assert [c[3] for c in fake.calls if c[:2] == ("POST", "/release")] == [{"guid": "g1", "indexerId": 1}]
    assert told[-1] == (2, "✅ Dune (2021): freigegeben, der Download startet.")


def test_admin_never_waits_and_reject_removes(tmp_path):
    big = [{"guid": "g1", "indexerId": 1, "size": 58 * GB}]
    fake, tools, asked, told = setup(tmp_path, big)
    tools.approvals.add(None, "movie", min_gb=20)
    add(tools, 1)  # admin
    assert not asked
    try:
        add(tools, 2)
    except Held:
        pass
    asyncio.run(tools.decide(asked[0].id, False))
    assert ("DELETE", "/movie/7") in [c[:2] for c in fake.calls]
    assert told[-1][1] == "🙅 Dune (2021): Der Admin hat den Wunsch abgelehnt."
    text = approval.render_rules(tools.approvals.rules(), tools.approvals.pending(), str)
    assert "Filme von allen, wenn der Download größer als 20 GB ist" in text


def test_buttons_survive_restart(tmp_path):
    import json

    from arrbot.tools import Offer, Step

    fake, tools, asked, told = setup(tmp_path, [])
    tools.offers["o1"] = Offer("o1", 2, [Candidate("movie", 1, "Dune", 2021, genres=["Drama"])], message_id=5, bulk=True)
    flow = Flow("f1", 2, Candidate("series", 9, "Severance", 2022, seasons=[1, 2]), stage="seasons")
    flow.step = Step("Welche Staffeln?", [("Alle", "all"), ("Staffel 1", "s1")])
    tools.flows["f1"] = flow
    tools.flows["f2"] = Flow("f2", 2, Candidate("series", 9, "X", None), stage="gfix", data=object())
    saved = json.loads(json.dumps(tools.dump_buttons()))

    _, fresh, _, _ = setup(tmp_path, [])
    assert fresh.load_buttons(saved) == 2  # the gfix flow with a plan is not kept
    assert fresh.offers["o1"].cands[0].genres == ["Drama"] and fresh.offers["o1"].bulk
    f = fresh.flows["f1"]
    assert f.cand.seasons == [1, 2] and f.step.buttons[1] == ("Staffel 1", "s1")
    assert asyncio.run(fresh.choose("f1", "s1")).cand.title == "Severance"
