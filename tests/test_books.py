"""📚 Books via Chaptarr: find, send the file into the chat, or add + search and send it when it arrives."""

import asyncio
import json

import httpx

from arrbot import books
from arrbot.chaptarr import Chaptarr
from arrbot.config import ArrConfig
from arrbot.llm import ToolCall
from arrbot.state import Store
from arrbot.tools import Flow, ToolRunner, View

CHAT = 5


def fake_chaptarr(lib_dir, calls):
    state = {"added": False, "file": False}
    schwarm = {"title": "Der Schwarm", "foreignBookId": "hc:1", "titleSlug": "der-schwarm",
               "author": {"authorName": "Frank Schätzing", "foreignAuthorId": "hc:9"}, "releaseDate": "2004-01-01",
               "editions": [{"foreignEditionId": "hc:e1", "monitored": True}]}
    limit = {"id": 7, "title": "Limit", "foreignBookId": "hc:2", "author": {"authorName": "Frank Schätzing"}}

    def handler(req):
        p, m = req.url.path, req.method
        calls.append((m, p, req.content.decode() if req.content else ""))
        if p == "/api/v1/book/lookup":
            term = req.url.params["term"]
            if "Limit" in term:
                return httpx.Response(200, json=[limit])
            return httpx.Response(200, json=[schwarm, dict(limit, title="Limit (Sonderausgabe)", id=0)])
        if p == "/api/v1/bookfile":
            bid = req.url.params["bookId"]
            if bid == "7" or (bid == "11" and state["file"]):
                return httpx.Response(200, json=[
                    {"path": "/books/Frank/limit.pdf", "size": 10},
                    {"path": "/books/Frank/limit.epub", "size": 10}] if bid == "7" else
                    [{"path": "/books/Frank/schwarm.epub", "size": 10}])
            return httpx.Response(200, json=[])
        if p == "/api/v1/rootfolder":
            return httpx.Response(200, json=[{"path": "/books", "isEffectiveDefaultEbook": True,
                                              "ebookQualityProfileId": 3, "ebookMetadataProfileId": 4}])
        if p == "/api/v1/book/import" and m == "POST":
            state["added"] = True
            return httpx.Response(200, content=b"")
        if p == "/api/v1/book" and m == "GET":
            return httpx.Response(200, json=[{"id": 11, "title": "Der Schwarm"}] if state["added"] else [])
        if p in ("/api/v1/book/monitor", "/api/v1/command"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    return Chaptarr(ArrConfig("http://c", "k"), transport=httpx.MockTransport(handler)), state


def setup(tmp_path):
    lib = tmp_path / "lib" / "Frank"
    lib.mkdir(parents=True)
    for n in ("limit.pdf", "limit.epub", "schwarm.epub"):
        (lib / n).write_bytes(b"x" * 10)
    calls = []
    tools = ToolRunner(None, None, Store(str(tmp_path)), "released")
    tools.chaptarr, state = fake_chaptarr(lib, calls)
    tools.books = books.PendingBooks(str(tmp_path))
    tools.book_paths = f"/books={tmp_path / 'lib'}".replace("\\", "/")
    return tools, state, calls


def test_book_in_library_is_sent_right_away(tmp_path):
    tools, _, _ = setup(tmp_path)
    res = asyncio.run(tools.run(ToolCall("1", "book", {"title": "Limit", "author": "Schätzing"}), CHAT))
    assert res.final.startswith("📚 Hier ist „Limit – Frank Schätzing“")
    view = next(x for x in tools.take_outbox(CHAT) if isinstance(x, View))
    assert view.kind == "file" and [n for _, n in view.arg] == ["limit.epub", "limit.pdf"]  # epub first


def test_pick_add_search_then_deliver(tmp_path):
    tools, state, calls = setup(tmp_path)
    asyncio.run(tools.run(ToolCall("1", "book", {"title": "Der Schwarm"}), CHAT))
    flow = next(x for x in tools.take_outbox(CHAT) if isinstance(x, Flow))
    assert flow.step.buttons[0] == ("Der Schwarm – Frank Schätzing (2004)", "b0") and flow.step.buttons[-1][1] == "no"
    flow = asyncio.run(tools.choose(flow.id, "b0"))
    assert flow.step.done and "schicke es dir, sobald es da ist" in flow.step.text
    body = json.loads(next(c for m, p, c in calls if p == "/api/v1/book/import"))
    assert body["foreignBookId"] == "hc:1" and body["qualityProfileId"] == 3 and body["rootFolder"] == "/books"
    assert body["authorMonitoring"]["monitor"] == "none" and body["foreignEditionId"] == "hc:e1"
    assert json.loads(next(c for m, p, c in calls if p == "/api/v1/command")) == {"name": "BookSearch", "bookIds": [11]}
    assert asyncio.run(tools.book_deliveries()) == []  # not there yet
    assert books.PendingBooks(str(tmp_path)).items[0].book_id == 11  # survives a restart
    state["file"] = True
    (chat, text, files), = asyncio.run(tools.book_deliveries())
    assert chat == CHAT and "ist da" in text and [n for _, n in files] == ["schwarm.epub"]
    assert tools.books.items == []


def test_paths_and_limits(tmp_path):
    assert books.map_path("/books/a/b.epub", "/data=/x;/books=/mnt/books") == "/mnt/books/a/b.epub"
    assert books.map_path("/other/b.epub", "/books=/mnt") == "/other/b.epub"
    big = tmp_path / "big.epub"
    big.write_bytes(b"x" * (2 * 2**20))
    send, problems = books.pick_files([{"path": str(big)}], None, max_mb=1)
    assert send == [] and "Telegram erlaubt Bots nur 1 MB" in problems[0]
    send, problems = books.pick_files([{"path": "/nope/x.epub"}], None)
    assert send == [] and "BOOK_PATHS" in problems[0]


def test_not_configured(tmp_path):
    tools = ToolRunner(None, None, Store(str(tmp_path)), "released")
    res = asyncio.run(tools.run(ToolCall("1", "book", {"title": "X"}), CHAT))
    assert "CHAPTARR_URL" in res.final
