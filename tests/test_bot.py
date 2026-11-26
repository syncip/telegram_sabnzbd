"""Offline tests: fake Radarr/Sonarr over httpx.MockTransport, scripted LLM."""

import asyncio
import json
from datetime import timedelta, timezone

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
from arrbot.tools import ToolRunner, View
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


def test_openai_reasoning_setting_falls_back_for_older_models():
    import openai

    from arrbot.llm import MAX_OUTPUT_TOKENS, REASONING_EXTRA_TOKENS, ToolSpec, Turn

    sent = []

    def handler(req):
        body = json.loads(req.content)
        sent.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": {
                "message": "Unsupported parameter: 'reasoning_effort' is not supported with this model.",
                "type": "invalid_request_error", "param": "reasoning_effort", "code": "unsupported_parameter"}})
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "ping", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55}})

    o = OpenAICompatProvider(LLMConfig("openai", "gpt-4o-mini", "k", reasoning="low"))
    o.client = openai.AsyncOpenAI(api_key="k", max_retries=0,
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ping = ToolSpec("ping", "x", {"type": "object", "properties": {}})
    turn = asyncio.run(o.complete("s", [Turn("user", text="hi")], [ping]))
    assert turn.tool_calls[0].name == "ping" and turn.usage["input"] == 50
    assert sent[0]["reasoning_effort"] == "low"
    assert sent[0]["max_completion_tokens"] == MAX_OUTPUT_TOKENS + REASONING_EXTRA_TOKENS
    assert sent[1]["reasoning_effort"] == "none" and sent[1]["max_completion_tokens"] == MAX_OUTPUT_TOKENS
    assert "reasoning_effort" not in sent[2]
    asyncio.run(o.complete("s", [Turn("user", text="hi")], [ping]))
    assert len(sent) == 4 and "reasoning_effort" not in sent[3]  # remembered: no second failed try


def _responses_reply(body, output, usage=(50, 0, 5)):
    return httpx.Response(200, json={
        "id": "resp_1", "object": "response", "created_at": 0, "model": body["model"], "status": "completed",
        "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": usage[0], "input_tokens_details": {"cached_tokens": usage[1]},
                  "output_tokens": usage[2], "output_tokens_details": {"reasoning_tokens": 0},
                  "total_tokens": usage[0] + usage[2]}})


def test_openai_uses_responses_api_with_thinking_and_tools():
    import openai

    from arrbot.llm import (MAX_OUTPUT_TOKENS, REASONING_EXTRA_TOKENS, OpenAIResponsesProvider, ToolSpec, Turn,
                            make_provider, uses_responses)

    assert isinstance(make_provider(LLMConfig("openai", "gpt-6-luna", "k")), OpenAIResponsesProvider)
    assert not uses_responses(LLMConfig("openai", "llama3", None, base_url="http://ollama:11434/v1"))
    assert uses_responses(LLMConfig("openai", "x", "k", base_url="https://api.openai.com/v1"))
    assert not uses_responses(LLMConfig("openai", "gpt-6-luna", "k", api="chat"))
    assert not uses_responses(LLMConfig("gemini", "g", "k"))

    sent = []

    def handler(req):
        assert req.url.path.endswith("/responses")
        body = json.loads(req.content)
        sent.append(body)
        if len(sent) == 1:
            return _responses_reply(body, [
                {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "GEHEIM"},
                {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "ping",
                 "arguments": '{"x": 1}', "status": "completed"}], usage=(3000, 2048, 120))
        return _responses_reply(body, [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                                        "content": [{"type": "output_text", "text": "Fertig.", "annotations": []}]}])

    o = make_provider(LLMConfig("openai", "gpt-6-luna", "k", reasoning="low"))
    o.client = openai.AsyncOpenAI(api_key="k", max_retries=0,
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ping = ToolSpec("ping", "x", {"type": "object", "properties": {}})
    history = [Turn("user", text="hi")]
    t1 = asyncio.run(o.complete("sys", history, [ping]))
    assert t1.tool_calls[0].id == "call_1" and t1.tool_calls[0].args == {"x": 1}
    assert t1.usage == {"input": 3000, "cached": 2048, "output": 120}
    b = sent[0]
    assert b["instructions"] == "sys" and b["store"] is False and b["reasoning"] == {"effort": "low"}
    assert b["include"] == ["reasoning.encrypted_content"]
    assert b["max_output_tokens"] == MAX_OUTPUT_TOKENS + REASONING_EXTRA_TOKENS
    assert b["tools"][0]["name"] == "ping" and b["tools"][0]["type"] == "function"

    history += [t1, Turn("tool", tool_results=[ToolResult("call_1", "ping", "pong")])]
    t2 = asyncio.run(o.complete("sys", history, [ping]))
    assert t2.text == "Fertig." and not t2.tool_calls
    items = sent[1]["input"]
    assert items[0] == {"role": "user", "content": "hi"}
    assert items[1]["type"] == "reasoning" and items[1]["encrypted_content"] == "GEHEIM"  # thinking goes along
    assert items[2] == {"type": "function_call", "call_id": "call_1", "name": "ping", "arguments": '{"x": 1}'}
    assert items[3] == {"type": "function_call_output", "call_id": "call_1", "output": "pong"}


def test_openai_responses_falls_back_without_reasoning_for_older_models():
    import openai

    from arrbot.llm import MAX_OUTPUT_TOKENS, ToolSpec, Turn, make_provider

    sent = []

    def handler(req):
        body = json.loads(req.content)
        sent.append(body)
        if "reasoning" in body:
            return httpx.Response(400, json={"error": {
                "message": "Unsupported parameter: 'reasoning.effort' is not supported with this model.",
                "type": "invalid_request_error", "param": "reasoning.effort", "code": "unsupported_parameter"}})
        return _responses_reply(body, [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                                        "content": [{"type": "output_text", "text": "ok", "annotations": []}]}])

    o = make_provider(LLMConfig("openai", "gpt-4o-mini", "k", reasoning="low"))
    o.client = openai.AsyncOpenAI(api_key="k", max_retries=0,
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ping = ToolSpec("ping", "x", {"type": "object", "properties": {}})
    assert asyncio.run(o.complete("s", [Turn("user", text="hi")], [ping])).text == "ok"
    assert "reasoning" not in sent[1] and "include" not in sent[1] and sent[1]["max_output_tokens"] == MAX_OUTPUT_TOKENS
    asyncio.run(o.complete("s", [Turn("user", text="hi")], [ping]))
    assert len(sent) == 3 and "reasoning" not in sent[2] and not o.reasoning_ok


def test_openai_thinking_model_with_tools_gets_reasoning_none():
    """gpt-6-luna: tools in Chat Completions only with reasoning_effort "none" (leaving it out = thinks)."""
    import openai

    from arrbot.llm import ToolSpec, Turn

    sent = []

    def handler(req):
        body = json.loads(req.content)
        sent.append(body)
        if body.get("reasoning_effort") != "none":
            return httpx.Response(400, json={"error": {
                "message": "Function tools with reasoning_effort are not supported for gpt-6-luna in "
                           "/v1/chat/completions. To use function tools, use /v1/responses or set "
                           "reasoning_effort to 'none'.", "type": "invalid_request_error"}})
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55}})

    o = OpenAICompatProvider(LLMConfig("openai", "gpt-6-luna", "k", reasoning="low"))
    o.client = openai.AsyncOpenAI(api_key="k", max_retries=0,
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ping = ToolSpec("ping", "x", {"type": "object", "properties": {}})
    assert asyncio.run(o.complete("s", [Turn("user", text="hi")], [ping])).text == "ok"
    assert [b.get("reasoning_effort") for b in sent] == ["low", "none"]
    asyncio.run(o.complete("s", [Turn("user", text="hi")], [ping]))
    assert sent[2]["reasoning_effort"] == "none"  # remembered


class Library:
    """Sonarr/Radarr with titles already in the library (for "such nochmal")."""

    def __init__(self):
        self.series = {5: {"id": 5, "title": "Married... with Children", "year": 1987, "monitored": False,
                           "alternateTitles": [{"title": "Eine schrecklich nette Familie"}],
                           "seasons": [{"seasonNumber": 0, "monitored": False}, {"seasonNumber": 1, "monitored": False},
                                       {"seasonNumber": 2, "monitored": False}]},
                       6: {"id": 6, "title": "Married at First Sight", "year": 2014, "monitored": True, "seasons": []}}
        self.movies = {9: {"id": 9, "title": "Dune", "year": 2021, "monitored": False, "hasFile": False}}
        self.eps = [
            {"id": 1, "seasonNumber": 0, "episodeNumber": 1, "airDateUtc": "1989-01-01T00:00:00Z", "hasFile": False},
            {"id": 11, "seasonNumber": 1, "episodeNumber": 1, "airDateUtc": "1987-04-05T00:00:00Z", "hasFile": False},
            {"id": 12, "seasonNumber": 1, "episodeNumber": 2, "airDateUtc": "1987-04-12T00:00:00Z", "hasFile": True,
             "episodeFileId": 3},
            {"id": 21, "seasonNumber": 2, "episodeNumber": 1, "airDateUtc": "1987-09-27T00:00:00Z", "hasFile": False},
            {"id": 22, "seasonNumber": 2, "episodeNumber": 2, "hasFile": False},  # no air date yet
        ]
        self.commands, self.monitored, self.puts = [], [], []

    def handler(self, req):
        p = req.url.path.removeprefix("/api/v3")
        body = json.loads(req.content) if req.content else None
        if p == "/series" and req.method == "GET":
            return httpx.Response(200, json=list(self.series.values()))
        if p == "/movie" and req.method == "GET":
            return httpx.Response(200, json=list(self.movies.values()))
        for path, lib in (("/series/", self.series), ("/movie/", self.movies)):
            if p.startswith(path) and p.split("/")[-1].isdigit():
                if req.method == "PUT":
                    self.puts.append(body)
                    lib[body["id"]] = body
                return httpx.Response(200, json=lib[int(p.split("/")[-1])])
        if p in ("/series/lookup", "/movie/lookup"):
            return httpx.Response(200, json=[])
        if p == "/episode":
            return httpx.Response(200, json=self.eps)
        if p == "/episode/monitor":
            self.monitored += body["episodeIds"]
            return httpx.Response(202, json={})
        if p == "/command":
            self.commands.append(body)
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404, text=f"unhandled {req.method} {p}")


def test_search_again_finds_title_monitors_and_searches_missing_episodes(tmp_path):
    lib = Library()
    radarr = Radarr(ArrConfig("http://r", "k"), transport=httpx.MockTransport(lib.handler))
    sonarr = Sonarr(ArrConfig("http://s", "k"), transport=httpx.MockTransport(lib.handler))
    tools = ToolRunner(radarr, sonarr, Store(str(tmp_path)), "released")

    # German title (alternate title), unmonitored series: monitored first, then only aired missing episodes.
    res = asyncio.run(tools.run(ToolCall("x", "search_existing",
                                         {"kind": "series", "title": "Eine schrecklich nette Familie"}), 42))
    assert not res.is_error, res.content
    # Season 2 is missing completely (aired): season search for a pack; season 1 only the missing episode.
    assert lib.commands == [{"name": "SeasonSearch", "seriesId": 5, "seasonNumber": 2},
                            {"name": "EpisodeSearch", "episodeIds": [11]}]  # no special, no file, no future
    assert lib.series[5]["monitored"] and [s["monitored"] for s in lib.series[5]["seasons"]] == [False, True, True]
    assert sorted(lib.monitored) == [11, 21]
    assert "2 fehlende Folgen" in res.final and "nicht überwacht" in res.final

    # Only one season.
    lib.commands.clear()
    asyncio.run(tools.run(ToolCall("x", "search_existing", {"kind": "series", "lib_id": 5, "seasons": [2]}), 42))
    assert lib.commands == [{"name": "SeasonSearch", "seriesId": 5, "seasonNumber": 2}]

    # Ambiguous title -> the AI gets the candidates.
    res = asyncio.run(tools.run(ToolCall("x", "search_existing", {"kind": "series", "title": "Married"}), 42))
    assert res.is_error and "lib_id=5" in res.content and "lib_id=6" in res.content

    # Not in the library.
    res = asyncio.run(tools.run(ToolCall("x", "search_existing", {"kind": "series", "title": "Severance"}), 42))
    assert res.is_error and "nicht in der Bibliothek" in res.content

    # Nothing missing.
    lib.eps = [e for e in lib.eps if e["id"] == 12]
    res = asyncio.run(tools.run(ToolCall("x", "search_existing", {"kind": "series", "lib_id": 5}), 42))
    assert "keine bereits erschienene Folge" in res.final

    # Movie: unmonitored -> monitored, then searched.
    lib.commands.clear()
    res = asyncio.run(tools.run(ToolCall("x", "search_existing", {"kind": "movie", "title": "dune"}), 42))
    assert lib.movies[9]["monitored"] and lib.commands == [{"name": "MoviesSearch", "movieIds": [9]}]
    assert "Neue Suche für Dune (2021)" in res.final


def test_series_job_one_start_message_and_one_summary(tmp_path):
    from arrbot.tracker import Tracker as Tr

    fr, fs, radarr, sonarr, store = make(tmp_path)
    sent = []

    async def send(chat, text, kb=None):
        sent.append(text)

    tr = Tr(radarr, sonarr, store, send, no_release_minutes=30, track_days=30)
    store.track("sonarr", 3, "Eine schrecklich nette Familie", 42)
    asyncio.run(tr.poll())
    now = utcnow().isoformat()

    def ep(i, e, event):
        return {"id": i, "seriesId": 3, "eventType": event, "date": now,
                "episode": {"seasonNumber": 1, "episodeNumber": e}}

    fs.queue = [{"seriesId": 3}]
    fs.history = [ep(e, e, "grabbed") for e in (1, 2)]
    asyncio.run(tr.poll())
    assert len(sent) == 1 and "Download gestartet" in sent[0] and "Zusammenfassung" in sent[0]
    for start in (3, 8):  # many more grabs and imports over several polls: silent
        fs.history = [ep(100 + e, e, "grabbed") for e in range(start, start + 5)] + \
                     [ep(200 + e, e, "downloadFolderImported") for e in range(1, start)]
        asyncio.run(tr.poll())
    assert len(sent) == 1
    fs.history = [ep(300 + e, e, "downloadFolderImported") for e in range(1, 13)]
    asyncio.run(tr.poll())
    assert len(sent) == 1  # still in the queue
    fs.queue = []
    asyncio.run(tr.poll())
    assert len(sent) == 1  # queue empty, but news came in less than 3 minutes ago
    store.state.tracked["sonarr:3"].job["last"] = (utcnow() - timedelta(minutes=4)).isoformat()
    asyncio.run(tr.poll())
    assert len(sent) == 2
    assert "12 von 12 Folgen fertig importiert (S01E01–S01E12)" in sent[1]
    assert not store.state.tracked["sonarr:3"].job


def test_mute_collects_messages_and_sums_up(tmp_path):
    from arrbot.tools import mute
    from arrbot.tracker import Tracker as Tr

    fr, fs, radarr, sonarr, store = make(tmp_path)
    sent = []

    async def send(chat, text, kb=None):
        sent.append((chat, text))

    tr = Tr(radarr, sonarr, store, send, no_release_minutes=30, track_days=30)
    store.track("radarr", 7, "Dune: Part Two (2024)", 42)
    store.track("radarr", 7, "Dune: Part Two (2024)", 43)
    asyncio.run(tr.poll())
    assert "Stumm für 30 Minuten" in mute(store, 42, 30)
    fr.history = [{"id": 1, "movieId": 7, "eventType": "grabbed", "date": utcnow().isoformat()}]
    asyncio.run(tr.poll())
    assert [c for c, _ in sent] == [43]  # 42 is muted, 43 is not
    store.state.muted["42"]["until"] = (utcnow() - timedelta(seconds=1)).isoformat()
    asyncio.run(tr.poll())
    assert sent[-1][0] == 42 and "1 Meldung:" in sent[-1][1] and "Download gestartet: Dune" in sent[-1][1]
    assert "42" not in store.state.muted
    assert "nicht stummgeschaltet" in mute(store, 42, None)


def test_notification_settings_skip_start_messages(tmp_path):
    from arrbot.tracker import Tracker as Tr

    fr, fs, radarr, sonarr, store = make(tmp_path)
    sent = []

    async def send(chat, text, kb=None):
        sent.append((chat, text))

    tools = ToolRunner(radarr, sonarr, store, "released")
    res = asyncio.run(tools.run(ToolCall("1", "notification_settings", {"start": False, "done": True}), 42))
    assert res.final == "" and tools.take_outbox(42) == [View(42, "notify")]
    assert store.state.notify == {"42": {"start": False}}
    tr = Tr(radarr, sonarr, store, send, no_release_minutes=30, track_days=30)
    store.track("radarr", 7, "Dune: Part Two (2024)", 42)
    store.track("radarr", 7, "Dune: Part Two (2024)", 43)
    asyncio.run(tr.poll())
    fr.history = [{"id": 1, "movieId": 7, "eventType": "grabbed", "date": utcnow().isoformat()}]
    asyncio.run(tr.poll())
    assert [c for c, _ in sent] == [43]  # 42 wants no "download started"
    res = asyncio.run(tools.run(ToolCall("2", "mute_notifications", {"days": 3}), 42))
    assert res.final.startswith("🔕 Stumm für 3 Tage.")


def test_debug_log_records_message_tools_and_requests(tmp_path):
    from arrbot import debuglog

    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released")
    llm = ScriptedLLM([
        Turn("assistant", tool_calls=[ToolCall("1", "search_movies", {"query": "Dune 2", "year": 2024})]),
        Turn("assistant", text="Gefunden.", usage={"input": 100, "output": 5}),
    ])
    agent = Agent(llm, tools, 30, 40)
    asyncio.run(agent.handle(777, "such dune 2"))
    icons = [e.icon for e in debuglog.LOG.entries(777)]
    texts = " | ".join(e.text for e in debuglog.LOG.entries(777))
    assert icons[0] == "💬" and "such dune 2" in texts
    assert "🔧" in icons and 'search_movies({"query": "Dune 2", "year": 2024})' in texts
    assert "Radarr GET /movie/lookup" in texts and "→ 200" in texts
    assert "↩️" in icons and "📤" in icons and "Gefunden." in texts
    assert debuglog.CURRENT.get() is None  # reset after the message
    html_ = debuglog.LOG.render(777, timezone.utc)
    assert html_.startswith("🐞 <b>Log</b>") and "<pre>" in html_


def test_diagnose_explains_why_nothing_was_loaded():
    from arrbot import diagnose

    assert diagnose.grabbed("Season search completed. 0 reports downloaded.") == 0
    assert diagnose.grabbed("Episode search completed. 3 reports downloaded.") == 3
    assert diagnose.grabbed("Completed") is None

    rel = [
        {"rejected": True, "rejections": ["Quality WEBDL-1080p is not wanted in profile"],
         "quality": {"quality": {"name": "DVD"}}, "languages": [{"name": "German"}]},
        {"rejected": True, "rejections": [{"reason": "Language is not wanted"}],
         "quality": {"quality": {"name": "WEBDL-1080p"}}, "languages": [{"name": "English"}]},
    ]
    lines = diagnose.summarize_releases(rel)
    assert "2 Releases – 0 passend, 2 abgelehnt" in lines[0]
    assert "1× DVD (Deutsch)" in lines[1] and "1× WEBDL-1080p (Englisch)" in lines[1]
    assert "Qualität nicht im Profil" in lines[2] and "Sprache nicht gewünscht" in lines[2]

    ix = [{"name": "scenenzbs", "enableAutomaticSearch": True,
           "fields": [{"name": "categories", "value": [5000, 5100]}]},
          {"name": "gut", "enableAutomaticSearch": True, "fields": [{"name": "categories", "value": [5000, 5030]}]}]
    hint = diagnose.category_hint(ix)
    assert "scenenzbs (5000, 5100)" in hint and "gut" not in hint

    class Svc:
        kind = "Sonarr"

        async def get(self, _):
            return {"title": "Married... with Children", "monitored": False,
                    "seasons": [{"seasonNumber": 1, "monitored": False}]}

        async def releases(self, **_):
            return []

        async def _req(self, *_a, **_k):
            return ix

    text = asyncio.run(diagnose.why_nothing(Svc(), "series", 5, 1))
    assert "nicht überwacht" in text and "Staffel 1 ist nicht überwacht" in text
    assert "liefern für Staffel 1 gar nichts" in text and "scenenzbs (5000, 5100)" in text


def test_search_command_is_watched_until_done(tmp_path):
    from arrbot import debuglog

    calls = []

    def handler(req):
        p = req.url.path.removeprefix("/api/v3")
        if p == "/command" and req.method == "POST":
            return httpx.Response(201, json={"id": 9, "status": "queued"})
        if p == "/command/9":
            calls.append(1)
            st = "started" if len(calls) < 2 else "completed"
            return httpx.Response(200, json={"id": 9, "status": st,
                                             "message": "Season search completed. 0 reports downloaded."})
        return httpx.Response(404)

    sonarr = Sonarr(ArrConfig("http://s", "k"), transport=httpx.MockTransport(handler))
    sonarr.COMMAND_EVERY = 0
    done = []

    async def on_done(chat, name, body, state):
        done.append((chat, name, body, state["message"]))

    sonarr.on_search_done = on_done

    async def go():
        token = debuglog.LOG.begin(555, "such nochmal")
        try:
            await sonarr.command("SeasonSearch", seriesId=5, seasonNumber=1)
        finally:
            debuglog.LOG.end(token)
        await sonarr.command("RefreshSeries", seriesId=5)  # no chat, no search: not watched
        from arrbot.arr import _WATCHERS
        await asyncio.gather(*list(_WATCHERS))

    asyncio.run(go())
    assert done == [(555, "SeasonSearch", {"seriesId": 5, "seasonNumber": 1},
                     "Season search completed. 0 reports downloaded.")]
    assert any("0 reports downloaded" in e.text for e in debuglog.LOG.entries(555))
