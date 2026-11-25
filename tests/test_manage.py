"""German episodes check/fix, deleting episodes, status of one title (all offline)."""

import asyncio
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from arrbot import downloads, manage
from arrbot.arr import Sonarr
from arrbot.config import ArrConfig
from arrbot.llm import ToolCall
from arrbot.state import Store
from arrbot.tools import Flow, ToolRunner, View

TODAY = date(2026, 11, 25)


class SeriesLib:
    """Sonarr with one series (id 4): S01E01 German, E02 English file, E03 missing, E04 not aired yet."""

    def __init__(self, releases=None):
        self.calls: list[tuple[str, str, object]] = []
        self.releases = releases if releases is not None else [
            {"title": "Show.S01.GERMAN.DL.1080p", "guid": "pack", "indexerId": 1, "fullSeason": True,
             "approved": True, "languages": [{"name": "German"}]},
            {"title": "Show.S01E03.GERMAN.720p", "guid": "e3", "indexerId": 1, "episodeNumbers": [3],
             "languages": [{"name": "German"}]},
            {"title": "Show.S01E02.1080p", "guid": "en2", "indexerId": 1, "episodeNumbers": [2],
             "languages": [{"name": "English"}]},
        ]

    def handler(self, req):
        p = req.url.path.removeprefix("/api/v3")
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, p, body))
        if p == "/series/lookup":
            return httpx.Response(200, json=[{"tvdbId": 500, "title": "Show", "year": 2020, "id": 4,
                                              "seasons": [{"seasonNumber": 1}]}])
        if p == "/series/4":
            return httpx.Response(200, json={"id": 4, "tvdbId": 500, "title": "Show", "year": 2020})
        if p == "/episode" and req.method == "GET":
            def ep(n, air, langs=None):
                e = {"id": 100 + n, "seasonNumber": 1, "episodeNumber": n, "airDateUtc": f"{air}T20:00:00Z",
                     "hasFile": langs is not None}
                if langs is not None:
                    e["episodeFileId"] = 900 + n
                    e["episodeFile"] = {"size": 2**30, "languages": [{"name": x} for x in langs]}
                return e
            return httpx.Response(200, json=[ep(1, "2026-10-01", ["German"]), ep(2, "2026-10-08", ["English"]),
                                             ep(3, "2026-10-15"), ep(4, "2026-12-20")])
        if p == "/release" and req.method == "GET":
            return httpx.Response(200, json=self.releases)
        if p in ("/release", "/command") and req.method == "POST":
            return httpx.Response(201, json={"id": 1})
        if p in ("/episodefile/bulk", "/episode/monitor"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    def sent(self, method, path):
        return [b for m, p, b in self.calls if m == method and p == path]


def sonarr(lib):
    return Sonarr(ArrConfig("http://s", "k"), transport=httpx.MockTransport(lib.handler))


def test_plan_prefers_season_pack_and_replaces_english_file():
    lib = SeriesLib()
    plan = asyncio.run(manage.plan_german(sonarr(lib), 4, None, TODAY))
    assert plan.checked == 3  # E04 has not aired
    assert [e["episodeNumber"] for e in plan.german] == [1]
    assert [e["episodeNumber"] for e in plan.other_lang] == [2]
    assert [e["episodeNumber"] for e in plan.missing] == [3]
    # Only 2 needed and E02 has no German single release: the pack fills the gap.
    assert len(plan.grabs) == 1 and plan.grabs[0].pack and plan.grabs[0].release["guid"] == "pack"
    text, buttons = manage.render_plan(plan)
    assert "📁 1 nur in anderer Sprache (S01E02)" in text and "❌ 1 fehlen (S01E03)" in text
    assert [v for _, v in buttons] == ["all", "no"]  # the pack also covers E02: no "only missing" option

    out = asyncio.run(manage.run_fix(sonarr(lib), plan, only_missing=False))
    assert lib.sent("POST", "/release") == [{"guid": "pack", "indexerId": 1}]
    assert lib.sent("DELETE", "/episodefile/bulk") == [{"episodeFileIds": [902]}]  # the English file
    assert lib.sent("PUT", "/episode/monitor") == [{"episodeIds": [102, 103], "monitored": True}]
    assert "1 deutsche Download gestartet für 2 Folgen" in out and "1 Dateien in anderer Sprache entfernt" in out


def test_plan_without_pack_offers_only_missing():
    lib = SeriesLib(releases=[r for r in SeriesLib().releases if r["guid"] != "pack"])
    plan = asyncio.run(manage.plan_german(sonarr(lib), 4, 1, TODAY))
    assert [g.release["guid"] for g in plan.grabs] == ["e3"]
    assert [e["episodeNumber"] for e in plan.not_found] == [2]
    text, buttons = manage.render_plan(plan)
    assert "🔍 Noch nichts Deutsches für: S01E02" in text
    assert [v for _, v in buttons] == ["all", "no"]
    asyncio.run(manage.run_fix(sonarr(lib), plan, only_missing=True))
    assert lib.sent("DELETE", "/episodefile/bulk") == []  # E03 had no file, nothing to delete


def test_clean_not_german_and_drop():
    lib = SeriesLib()
    plan = asyncio.run(manage.plan_clean(sonarr(lib), 4, "not_german", None))
    assert [e["episodeNumber"] for e in plan.episodes] == [2]
    text, buttons = manage.render_clean(plan)
    assert text.startswith("🗑 Show: 1 Folgen nicht auf Deutsch (1,0 GB)")
    out = asyncio.run(manage.run_clean(sonarr(lib), plan, research=False))
    assert lib.sent("DELETE", "/episodefile/bulk") == [{"episodeFileIds": [902]}]
    assert lib.sent("PUT", "/episode/monitor") == [{"episodeIds": [102], "monitored": False}]
    assert "nicht mehr automatisch geladen" in out


def test_german_fix_tool_runs_after_button(tmp_path):
    lib = SeriesLib()
    store = Store(str(tmp_path))
    tools = ToolRunner(None, sonarr(lib), store, "released")
    tools.is_admin = lambda uid: True
    res = asyncio.run(tools.run(ToolCall("1", "german_fix", {"tvdb_id": 500}), 5))
    assert res.final == ""  # no AI wording, the bot shows the plan with buttons
    flow = tools.take_outbox(5)[0]
    assert isinstance(flow, Flow) and flow.stage == "gfix" and "2 Folgen" in flow.step.text
    assert lib.sent("POST", "/release") == []  # nothing happens before the button

    done = asyncio.run(tools.choose(flow.id, "all"))
    assert done.step.done and "deutsche Download gestartet" in done.step.text
    assert lib.sent("POST", "/release") == [{"guid": "pack", "indexerId": 1}]
    assert "sonarr:4" in store.state.tracked


def test_clean_tool_cancel_changes_nothing(tmp_path):
    lib = SeriesLib()
    tools = ToolRunner(None, sonarr(lib), Store(str(tmp_path)), "released")
    tools.is_admin = lambda uid: True
    asyncio.run(tools.run(ToolCall("1", "clean_episodes", {"tvdb_id": 500, "which": "all"}), 5))
    flow = tools.take_outbox(5)[0]
    done = asyncio.run(tools.choose(flow.id, "no"))
    assert "bleibt alles" in done.step.text and lib.sent("DELETE", "/episodefile/bulk") == []


def test_status_of_one_title(tmp_path):
    tools = ToolRunner(None, None, Store(str(tmp_path)), "released")
    asyncio.run(tools.run(ToolCall("1", "download_status", {"title": "Married with Children"}), 5))
    assert tools.take_outbox(5) == [View(5, "downloads", False, "Married with Children")]

    snap = downloads.Snapshot(
        items=[downloads.Item("radarr:1", "movie", "The Naked Gun (2025)", state=downloads.PROBLEM, owners=[5],
                              problems=["Found matching movie via grab history, but release was matched to movie by ID."])],
        searching=[("Married... with Children", [5])], finished=[])
    now = datetime(2026, 11, 25, 18, tzinfo=timezone.utc)
    text = downloads.render(snap, 5, False, str, ZoneInfo("Europe/Berlin"), now=now, query="married with children")
    assert "Status: married with children" in text and "Married... with Children" in text
    assert "Naked Gun" not in text
    text = downloads.render(snap, 5, False, str, ZoneInfo("Europe/Berlin"), now=now, query="Simpsons")
    assert "Zu „Simpsons“ läuft gerade nichts" in text
