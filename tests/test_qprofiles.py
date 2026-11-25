"""Quality profiles from plain language (offline)."""

import asyncio
import json

import httpx

from arrbot import qprofiles
from arrbot.arr import Radarr
from arrbot.config import ArrConfig
from arrbot.llm import ToolCall
from arrbot.state import Store
from arrbot.tools import ToolRunner


def q(i, name, res):
    return {"quality": {"id": i, "name": name, "resolution": res}, "items": [], "allowed": False}


TEMPLATE = {"id": 1, "name": "HD-1080p", "upgradeAllowed": False, "cutoff": 7, "minFormatScore": 0,
            "language": {"id": 1, "name": "English"},
            "items": [q(1, "SDTV", 480), q(4, "HDTV-720p", 720),
                      {"id": 1001, "name": "WEB 1080p", "allowed": True,
                       "items": [q(3, "WEBDL-1080p", 1080), q(15, "WEBRip-1080p", 1080)]},
                      q(7, "Bluray-1080p", 1080), q(19, "Bluray-2160p", 2160)],
            "formatItems": []}


class FakeRadarr:
    def __init__(self):
        self.formats = [{"id": 1, "name": "Sprache: Deutsch"}]
        self.created = []

    def handler(self, req):
        p = req.url.path.removeprefix("/api/v3")
        body = json.loads(req.content) if req.content else None
        if p == "/qualityprofile" and req.method == "GET":
            return httpx.Response(200, json=[{"id": 1, "name": "HD-1080p"}] + [{"id": 9, "name": c["name"]} for c in self.created])
        if p == "/qualityprofile/1":
            return httpx.Response(200, json=TEMPLATE)
        if p == "/qualityprofile" and req.method == "POST":
            self.created.append(body)
            return httpx.Response(201, json={**body, "id": 9})
        if p == "/customformat" and req.method == "GET":
            return httpx.Response(200, json=self.formats)
        if p == "/customformat" and req.method == "POST":
            cf = {**body, "id": len(self.formats) + 1}
            self.formats.append(cf)
            return httpx.Response(201, json=cf)
        if p == "/language":
            return httpx.Response(200, json=[{"id": -1, "name": "Any"}, {"id": -2, "name": "Original"},
                                             {"id": 1, "name": "English"}, {"id": 2, "name": "French"},
                                             {"id": 3, "name": "Spanish"}, {"id": 4, "name": "German"},
                                             {"id": 5, "name": "Italian"}])
        return httpx.Response(404)


def test_wish_from_words():
    w = qprofiles.make_wish("2K DE+EN+OV", None, 1080, 1440, ["Deutsch", "englisch"], ["Originalsprache", "deutsch"],
                            False)
    assert (w.min_res, w.max_res, w.required, w.preferred, w.services) == (
        1080, 1080, ["German", "English"], ["Original"], ["radarr", "sonarr"])
    assert "Pflicht: Deutsch + Englisch" in w.summary() and "Bevorzugt: Originalsprache" in w.summary()
    w = qprofiles.make_wish("4K Englisch only", "radarr", None, 4000, ["English"], [], True)
    assert w.max_res == 2160 and "Nie: Deutsch, Französisch, Spanisch, Italienisch" in w.summary()


def test_create_4k_english_only_after_confirmation(tmp_path):
    fake = FakeRadarr()
    radarr = Radarr(ArrConfig("http://r", "k"), transport=httpx.MockTransport(fake.handler))
    tools = ToolRunner(radarr, None, Store(str(tmp_path)), "released")
    tools.is_admin = lambda uid: True
    tools.users = object()  # admin tools are only offered with user management
    asyncio.run(tools.run(ToolCall("1", "admin_quality_profile", {
        "name": "4K Englisch", "min_resolution": 2160, "required_languages": ["Englisch"], "only": True}), 5))
    flow = tools.take_outbox(5)[0]
    assert flow.stage == "qprof" and "Für: Filme (Radarr)" in flow.step.text and not fake.created

    done = asyncio.run(tools.choose(flow.id, "yes"))
    assert "✅ Radarr: Profil „4K Englisch“ angelegt." in done.step.text
    p = fake.created[0]
    assert [i["allowed"] for i in p["items"]] == [False, False, False, False, True] and p["cutoff"] == 19
    scores = {f["name"]: f["score"] for f in p["formatItems"]}
    assert scores == {"Sprache: Deutsch": -10000, "Sprache: Englisch": 1000, "Sprache: Französisch": -10000,
                      "Sprache: Spanisch": -10000, "Sprache: Italienisch": -10000}
    assert p["minFormatScore"] == 1000 and p["language"] == {"id": -1, "name": "Any"} and "id" not in p
    eng = next(f for f in fake.formats if f["name"] == "Sprache: Englisch")
    assert eng["specifications"][0]["fields"] == [{"name": "value", "value": 1}]


def test_create_1080_german_english_plus_original():
    fake = FakeRadarr()
    radarr = Radarr(ArrConfig("http://r", "k"), transport=httpx.MockTransport(fake.handler))
    w = qprofiles.make_wish("FullHD DE+EN", "radarr", 1080, 1080, ["Deutsch", "Englisch"], ["Original"], False)
    assert asyncio.run(qprofiles.create(radarr, w)).startswith("✅")
    p = fake.created[0]
    group = p["items"][2]
    assert group["allowed"] and all(x["allowed"] for x in group["items"]) and p["cutoff"] == 7
    scores = {f["name"]: f["score"] for f in p["formatItems"]}
    assert scores == {"Sprache: Deutsch": 1000, "Sprache: Englisch": 1000, "Sprache: Originalsprache": 100}
    assert p["minFormatScore"] == 2000 and p["cutoffFormatScore"] == 2100
    # Same name again: nothing is changed
    assert "gibt es schon" in asyncio.run(qprofiles.create(radarr, w))
