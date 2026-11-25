"""Offline tests: fake Radarr/Sonarr over httpx.MockTransport, scripted LLM."""

import asyncio
import json
from datetime import timedelta

import httpx

from arrbot.agent import Agent
from arrbot.arr import Radarr, Sonarr
from arrbot.config import ArrConfig, LLMConfig
from arrbot.llm import (
    AnthropicProvider,
    GeminiProvider,
    LLMProvider,
    OpenAICompatProvider,
    ToolCall,
    ToolResult,
    Turn,
)
from arrbot.state import Store, utcnow
from arrbot.tools import ToolRunner
from arrbot.tracker import Tracker


class FakeArr:
    def __init__(self):
        self.movies: dict[int, dict] = {}
        self.series: dict[int, dict] = {}
        self.history: list[dict] = []
        self.queue: list[dict] = []
        self.commands: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, q = req.url.path.removeprefix("/api/v3"), req.url.params
        body = json.loads(req.content) if req.content else None
        if p == "/qualityprofile":
            def prof(res):
                return {"quality": {"resolution": res}, "allowed": True}
            return httpx.Response(200, json=[{"id": 1, "name": "HD-1080p", "items": [prof(720), prof(1080)]},
                                             {"id": 2, "name": "Ultra-HD", "items": [prof(1080), prof(2160)]}])
        if p == "/rootfolder":
            return httpx.Response(200, json=[{"path": "/media"}])
        if p == "/languageprofile":
            return httpx.Response(404)
        if p == "/movie/lookup":
            return httpx.Response(200, json=[
                {"tmdbId": 693134, "title": "Dune: Part Two", "year": 2024, "overview": "Paul..."},
                {"tmdbId": 438631, "title": "Dune", "year": 2021, "overview": "Paul Atreides..."},
            ])
        if p == "/movie/lookup/tmdb":
            m = {"tmdbId": int(q["tmdbId"]), "title": "Dune: Part Two", "year": 2024}
            return httpx.Response(200, json=m)
        if p == "/movie" and req.method == "POST":
            body["id"] = 7
            self.movies[7] = body
            return httpx.Response(201, json=body)
        if p == "/series/lookup":
            return httpx.Response(200, json=[{
                "tvdbId": 371980, "title": "Severance", "year": 2022, "status": "continuing",
                "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}, {"seasonNumber": 2}],
            }])
        if p == "/series" and req.method == "POST":
            body["id"] = 3
            self.series[3] = body
            return httpx.Response(201, json=body)
        if p.startswith("/series/") and req.method == "GET":
            return httpx.Response(200, json=self.series[int(p.split("/")[-1])])
        if p.startswith("/series/") and req.method == "PUT":
            self.series[int(p.split("/")[-1])] = body
            return httpx.Response(202, json=body)
        if p == "/command" and req.method == "GET":  # running commands: a refresh that is done
            return httpx.Response(200, json=[{"name": "RefreshSeries", "status": "completed", "body": {"seriesId": 3}}])
        if p == "/command":
            self.commands.append(body)
            return httpx.Response(201, json=body)
        if p == "/episode" and req.method == "GET":
            s = self.series.get(int(q["seriesId"]), {})
            return httpx.Response(200, json=[
                {"id": 1000 + x["seasonNumber"] * 10 + n, "seasonNumber": x["seasonNumber"], "episodeNumber": n}
                for x in s.get("seasons", []) for n in (1, 2)])
        if p == "/episode/monitor" and req.method == "PUT":
            self.monitored_episodes = getattr(self, "monitored_episodes", []) + body["episodeIds"]
            return httpx.Response(202, json={})
        if p == "/history/since":
            return httpx.Response(200, json=self.history)
        if p == "/queue":
            return httpx.Response(200, json={"records": self.queue})
        return httpx.Response(404, text=f"unhandled {req.method} {p}")


def make(tmp_path):
    fr, fs = FakeArr(), FakeArr()
    radarr = Radarr(ArrConfig("http://r", "k"), transport=httpx.MockTransport(fr.handler))
    sonarr = Sonarr(ArrConfig("http://s", "k"), transport=httpx.MockTransport(fs.handler))
    store = Store(str(tmp_path))
    return fr, fs, radarr, sonarr, store


class ScriptedLLM(LLMProvider):
    """Returns pre-scripted turns and records what it was given."""

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[list[Turn]] = []

    async def complete(self, system, history, tools):
        self.seen.append(list(history))
        return self.script.pop(0)


def test_agent_adds_movie_and_tracks(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released")
    llm = ScriptedLLM([
        Turn("assistant", tool_calls=[ToolCall("1", "search_movies", {"query": "Dune 2", "year": 2024})]),
        Turn("assistant", tool_calls=[ToolCall("2", "add_movie", {"tmdb_id": 693134})]),
    ])
    agent = Agent(llm, tools, 30, 40)
    reply = asyncio.run(agent.handle(42, "lad dune 2"))
    # The add ends the turn: no third AI call just to say "done", and the history is cleared.
    assert len(llm.seen) == 2 and llm.script == []
    assert reply.startswith("✅ Dune: Part Two (2024) hinzugefügt")
    # Only the question and a short answer stay, for follow-ups like "und der Status?".
    h = agent.convs[42].history
    assert [t.role for t in h] == ["user", "assistant"] and h[0].text == "lad dune 2"
    assert h[1].text.startswith("✅ Dune: Part Two") and not h[1].tool_calls
    search_result = json.loads(llm.seen[1][-1].tool_results[0].content)
    assert search_result == [{"tmdb": 693134, "title": "Dune: Part Two", "year": 2024}]  # compact, year filter
    added = fr.movies[7]
    assert added["qualityProfileId"] == 1 and added["rootFolderPath"] == "/media"
    assert added["addOptions"] == {"searchForMovie": False}
    assert fr.commands == [{"name": "MoviesSearch", "movieIds": [7]}]  # manual search like the button
    assert store.state.tracked["radarr:7"].chat_ids == [42]
    assert agent.usage.requests == 1 and agent.usage.calls == 2


def test_tool_error_is_reported_not_raised(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released")
    res = asyncio.run(tools.run(ToolCall("x", "add_movie", {"tmdb_id": 1, "quality_profile": "4K"}), 1))
    assert res.is_error and "HD-1080p" in res.content


def test_add_series_specific_seasons(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released")
    async def go():
        res = await tools.run(ToolCall("x", "add_series", {"tvdb_id": 371980, "seasons": [2]}), 5)
        await sonarr.wait_pending()  # monitoring + search run in the background after adding
        return res

    res = asyncio.run(go())
    assert not res.is_error, res.content
    s = fs.series[3]
    assert {x["seasonNumber"]: x["monitored"] for x in s["seasons"]} == {0: False, 1: False, 2: True}
    # The episodes themselves must be monitored, otherwise Sonarr's season search finds nothing.
    assert fs.monitored_episodes == [1021, 1022]
    assert fs.commands == [{"name": "SeasonSearch", "seriesId": 3, "seasonNumber": 2}]
    assert "languageProfileId" not in s


def test_tracker_notifications(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    sent: list[tuple[int, str]] = []

    async def send(chat, text):
        sent.append((chat, text))

    tr = Tracker(radarr, sonarr, store, send, no_release_minutes=30, track_days=30)
    store.track("radarr", 7, "Dune: Part Two (2024)", 42)
    store.track("sonarr", 3, "Severance", 42)

    asyncio.run(tr.poll())  # first poll only sets the cursor
    assert sent == []

    now = utcnow().isoformat()
    fr.history = [
        {"id": 1, "movieId": 7, "eventType": "grabbed", "date": now,
         "quality": {"quality": {"name": "Bluray-1080p"}}, "data": {"indexer": "NZBgeek"}},
        {"id": 2, "movieId": 99, "eventType": "grabbed", "date": now},  # not tracked
    ]
    fs.history = [
        {"id": 10 + e, "seriesId": 3, "eventType": "grabbed", "date": now, "episode": {"seasonNumber": 2, "episodeNumber": e}}
        for e in (1, 2, 3)
    ]
    asyncio.run(tr.poll())
    assert len(sent) == 2  # one for the movie, one grouped message for 3 episodes
    assert "Download gestartet: Dune" in sent[0][1] and "Bluray-1080p, NZBgeek" in sent[0][1]
    assert "S02E01, S02E02, S02E03" in sent[1][1]

    # Same records again must not notify twice.
    asyncio.run(tr.poll())
    assert len(sent) == 2

    fr.history = [{"id": 3, "movieId": 7, "eventType": "downloadFailed", "date": now, "data": {"message": "CRC error"}}]
    fr.queue = [{"movieId": 7, "movie": {"title": "Dune: Part Two", "year": 2024}, "downloadId": "abc",
                 "trackedDownloadStatus": "warning", "trackedDownloadState": "importBlocked",
                 "statusMessages": [{"messages": ["Kein passender Film gefunden"]}]}]
    asyncio.run(tr.poll())
    asyncio.run(tr.poll())
    texts = [t for _, t in sent[2:]]
    assert len(texts) == 2
    assert "fehlgeschlagen" in texts[0] and "CRC error" in texts[0]
    assert "Problem" in texts[1] and "Kein passender Film" in texts[1]

    fr.queue = []
    fr.history = [{"id": 4, "movieId": 7, "eventType": "downloadFolderImported", "date": now}]
    asyncio.run(tr.poll())
    assert "Fertig" in sent[-1][1]
    assert "radarr:7" not in store.state.tracked  # finished movies stop being tracked
    assert "sonarr:3" in store.state.tracked


def test_no_release_notice(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    sent = []

    buttons = []

    async def send(chat, text, kb=None):
        sent.append(text)
        buttons.append(kb)

    tr = Tracker(radarr, sonarr, store, send, no_release_minutes=30, track_days=30)
    t = store.track("radarr", 7, "Obscure Film (1950)", 1)
    t.added_at = (utcnow() - timedelta(minutes=31)).isoformat()
    asyncio.run(tr.poll())
    asyncio.run(tr.poll())
    assert len(sent) == 1 and "kein passender Download" in sent[0]  # only once
    assert ("⏸ In 3 Tagen nochmal", "n:p3:radarr:7") in buttons[0] and ("🗑 Löschen", "n:rm:radarr:7") in buttons[0]


HISTORY = [
    Turn("user", text="dune"),
    Turn("assistant", text="Suche…", tool_calls=[ToolCall("c1", "search_movies", {"query": "dune"})]),
    Turn("tool", tool_results=[ToolResult("c1", "search_movies", "[]")]),
]


def test_provider_wire_formats():
    g = GeminiProvider(LLMConfig("gemini", "gemini-flash-latest", "k"))
    contents = g._contents(HISTORY)
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[1].function_call.name == "search_movies"
    assert contents[2].parts[0].function_response.response == {"result": "[]"}

    a = AnthropicProvider(LLMConfig("anthropic", "claude-opus-5-5", "k"))
    msgs = a._messages(HISTORY)
    assert msgs[1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "search_movies", "input": {"query": "dune"}}
    assert msgs[2]["content"][0]["tool_use_id"] == "c1"

    o = OpenAICompatProvider(LLMConfig("openai", "gpt-4o-mini", "k"))
    om = o._messages("sys", HISTORY)
    assert om[0]["role"] == "system" and om[2]["tool_calls"][0]["function"]["name"] == "search_movies"
    assert om[3] == {"role": "tool", "tool_call_id": "c1", "content": "[]"}


def test_movie_quality_buttons_without_ai(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released", quality_prompt_seconds=60)

    res = asyncio.run(tools.run(ToolCall("x", "add_movie", {"tmdb_id": 693134}), 42))
    assert res.final == "" and not res.is_error and fr.movies == {}
    [flow] = tools.take_outbox(42)
    assert flow.stage == "quality" and flow.step.timeout == 60
    assert flow.step.buttons == [("⭐ HD-1080p", "p0"), ("Ultra-HD", "p1")]
    assert tools.take_outbox(42) == []

    flow = asyncio.run(tools.choose(flow.id, "p1"))
    assert flow.step.done and flow.step.text.startswith("✅") and fr.movies[7]["qualityProfileId"] == 2
    assert asyncio.run(tools.choose(flow.id, "p0")) is None  # stale button
    assert asyncio.run(tools.quality_timeout(flow.id)) is None
    assert "radarr:7" in store.state.tracked


def test_quality_timeout_takes_configured_default(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    radarr.cfg.quality_profile = "Ultra-HD"
    tools = ToolRunner(radarr, sonarr, store, "released", quality_prompt_seconds=60)
    asyncio.run(tools.run(ToolCall("x", "add_movie", {"tmdb_id": 693134}), 1))
    [flow] = tools.take_outbox(1)
    assert flow.default_profile == "Ultra-HD"
    flow = asyncio.run(tools.quality_timeout(flow.id))
    assert flow.step.done and fr.movies[7]["qualityProfileId"] == 2


def test_quality_limit_for_users(tmp_path):
    from arrbot.users import UserStore
    fr, fs, radarr, sonarr, store = make(tmp_path)
    radarr.cfg.quality_profile = "Ultra-HD"
    tools = ToolRunner(radarr, sonarr, store, "released", quality_prompt_seconds=60)
    tools.users = UserStore(str(tmp_path), admin_id=1, seed_users={5})
    tools.is_admin = tools.users.is_admin
    assert "höchstens Full HD" in tools.users.apply_quality("standard", "1080")
    assert asyncio.run(tools.run(ToolCall("l", "list_quality_profiles", {"kind": "movie"}), 5)).content == '["HD-1080p"]'
    res = asyncio.run(tools.run(ToolCall("x", "add_movie", {"tmdb_id": 693134, "quality_profile": "Ultra-HD"}), 5))
    assert res.is_error and "über deiner Grenze" in res.content and 7 not in fr.movies
    asyncio.run(tools.run(ToolCall("x", "add_movie", {"tmdb_id": 693134}), 5))  # one profile left: no question
    assert fr.movies[7]["qualityProfileId"] == 1
    assert tools.users.max_quality(1) == 0  # admins have no limit
    assert "keine Qualitätsgrenze" in tools.users.apply_quality("5", "aus")


def test_series_season_then_quality_buttons(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released", quality_prompt_seconds=60)
    asyncio.run(tools.run(ToolCall("s", "search_series", {"query": "severance"}), 1))
    res = asyncio.run(tools.run(ToolCall("x", "add_series", {"tvdb_id": 371980}), 1))
    assert res.final == ""
    [flow] = tools.take_outbox(1)
    assert flow.stage == "seasons" and flow.step.timeout == 0
    assert flow.step.buttons == [("Alle Staffeln", "all"), ("Staffel 1", "s1"), ("Staffel 2", "s2"),
                                 ("Nur neue Folgen", "future")]
    flow = asyncio.run(tools.choose(flow.id, "s2"))
    assert flow.stage == "quality" and "Staffel 2" in flow.step.text
    flow = asyncio.run(tools.choose(flow.id, "p0"))
    assert flow.step.done, flow.step.text
    assert {x["seasonNumber"]: x["monitored"] for x in fs.series[3]["seasons"]} == {0: False, 1: False, 2: True}


def test_explicit_choices_add_immediately(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released", quality_prompt_seconds=60)
    args = {"tvdb_id": 371980, "monitor": "all", "quality_profile": "ultra-hd"}
    res = asyncio.run(tools.run(ToolCall("y", "add_series", args), 42))
    assert res.final.startswith("✅") and fs.series[3]["qualityProfileId"] == 2
    assert tools.take_outbox(42) == []


def test_offer_titles_and_pick_without_ai(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released", quality_prompt_seconds=60)
    llm = ScriptedLLM([
        Turn("assistant", tool_calls=[ToolCall("1", "search_movies", {"query": "Dune"})]),
        Turn("assistant", tool_calls=[ToolCall("2", "offer_titles", {"kind": "movie", "ids": [693134, 438631]})]),
    ])
    agent = Agent(llm, tools, 30, 40)
    assert asyncio.run(agent.handle(1, "dune")) == ""  # buttons only, no extra AI text
    [offer] = tools.take_outbox(1)
    assert [c.label for c in offer.cands] == ["Dune: Part Two (2024)", "Dune (2021)"]
    offer.message_id = 99
    _, flow = asyncio.run(tools.choose_offer(offer.id, 1))
    assert flow.cand.ext_id == 438631 and flow.stage == "quality" and flow.message_id == 99
    assert asyncio.run(tools.choose_offer(offer.id, 0)) is None  # offer used up


def test_unwritable_data_dir_does_not_break_adding(tmp_path, monkeypatch):
    fr, fs, radarr, sonarr, store = make(tmp_path)

    def deny(*a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("arrbot.state.tempfile.mkstemp", deny)
    tools = ToolRunner(radarr, sonarr, store, "released")
    res = asyncio.run(tools.run(ToolCall("x", "add_movie", {"tmdb_id": 693134}), 42))
    assert not res.is_error, res.content
    assert fr.movies[7] and "radarr:7" in store.state.tracked and store.writable is False
