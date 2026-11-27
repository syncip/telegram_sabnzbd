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
    tools.book_formats = books.BookFormats(str(tmp_path))
    tools.book_formats.set(CHAT, "epub")
    tools.book_cache = str(tmp_path / "cache")
    tools.book_paths = f"/books={tmp_path / 'lib'}".replace("\\", "/")
    return tools, state, calls


def test_book_in_library_is_sent_right_away(tmp_path):
    tools, _, _ = setup(tmp_path)
    res = asyncio.run(tools.run(ToolCall("1", "book", {"title": "Limit", "author": "Schätzing"}), CHAT))
    assert res.final.startswith("📚 Hier ist „Limit – Frank Schätzing“")
    view = next(x for x in tools.take_outbox(CHAT) if isinstance(x, View))
    assert view.kind == "file" and [n for _, n in view.arg] == ["limit.epub"]  # the chosen format only


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


def fake_ops():
    """Chaptarr's system side: status, profiles, health, queue, disk space."""
    def handler(req):
        p = req.url.path
        if p == "/api/v1/system/status":
            return httpx.Response(200, json={"version": "0.9.1"})
        if p == "/api/v1/qualityprofile":
            return httpx.Response(200, json=[{"id": 1, "name": "eBook"}])
        if p == "/api/v1/health":
            return httpx.Response(200, json=[{"source": "IndexerStatusCheck", "type": "warning", "message": "Indexer weg"}])
        if p == "/api/v1/indexer":
            raise AssertionError("no sub category check for books")
        if p == "/api/v1/queue":
            assert req.url.params["includeBook"] == "true"
            return httpx.Response(200, json={"records": [
                {"id": 3, "bookId": 11, "book": {"title": "Der Schwarm"}, "author": {"authorName": "Frank Schätzing"},
                 "size": 100, "sizeleft": 50, "status": "downloading", "downloadId": "x1"}]})
        if p == "/api/v1/rootfolder":
            return httpx.Response(200, json=[{"path": "/books"}])
        if p == "/api/v1/diskspace":
            return httpx.Response(200, json=[{"path": "/books", "freeSpace": 2**40, "totalSpace": 2**41}])
        return httpx.Response(404)

    return Chaptarr(ArrConfig("http://c", "k"), transport=httpx.MockTransport(handler))


def test_chaptarr_in_check_health_downloads_disk(tmp_path):
    from datetime import timezone

    from arrbot import arrhealth, downloads, extras, health

    ch = fake_ops()
    checks = asyncio.run(health.run_checks(bot=None, provider=None, model="", radarr=None, sonarr=None, sab=None,
                                           tmdb=None, light=True, chaptarr=ch))
    (c,) = checks
    assert c.name == "Chaptarr" and c.ok and "0.9.1" in c.line and "1 Qualitätsprofile" in c.line
    (issue,) = asyncio.run(arrhealth.issues(ch))
    assert issue.service == "Chaptarr" and issue.source in arrhealth.AUTO_FIX
    snap = asyncio.run(downloads.collect(None, None, Store(str(tmp_path)), None, False, timezone.utc,
                                         chaptarr=ch, book_owners={11: [CHAT]}))
    (it,) = snap.items
    assert it.kind == "book" and it.title == "Der Schwarm – Frank Schätzing" and it.owners == [CHAT] and it.pct == 50
    assert "📚" in downloads.render(snap, CHAT, False, str, timezone.utc)
    (d,) = asyncio.run(extras.disks(None, None, ch))
    assert d.path == "/books" and d.free == 2**40
    # not set up: no line in /check, nothing breaks
    assert asyncio.run(health.run_checks(bot=None, provider=None, model="", radarr=None, sonarr=None, sab=None,
                                         tmdb=None, light=True)) == []


def test_format_asked_once_then_remembered(tmp_path):
    tools, _, _ = setup(tmp_path)
    tools.book_formats.by_chat.clear()
    asyncio.run(tools.run(ToolCall("1", "book", {"title": "Limit"}), CHAT))
    flow = next(x for x in tools.take_outbox(CHAT) if isinstance(x, Flow))
    assert "welchem Format" in flow.step.text and ("📄 PDF", "f:pdf") in flow.step.buttons
    flow = asyncio.run(tools.choose(flow.id, "f:pdf"))
    assert flow.step.done and "Hier ist" in flow.step.text
    view = next(x for x in tools.take_outbox(CHAT) if isinstance(x, View))
    assert [n for _, n in view.arg] == ["limit.pdf"]
    assert books.BookFormats(str(tmp_path)).get(CHAT) == "pdf"  # remembered
    res = asyncio.run(tools.run(ToolCall("2", "book", {"title": "Limit"}), CHAT))
    assert res.final.startswith("📚 Hier ist")  # no question the second time
    tools.take_outbox(CHAT)
    # "als Kindle": converted (fake Calibre), and the new default
    async def fake_convert(src, fmt, cache_dir):
        out = tmp_path / f"limit.{fmt}"
        out.write_bytes(b"k")
        return str(out)

    real = books.convert
    books.convert = fake_convert
    try:
        asyncio.run(tools.run(ToolCall("3", "book", {"title": "Limit", "format": "azw3"}), CHAT))
    finally:
        books.convert = real
    view = next(x for x in tools.take_outbox(CHAT) if isinstance(x, View))
    assert [n for _, n in view.arg] == ["limit.azw3"] and tools.book_formats.get(CHAT) == "azw3"


def test_no_calibre_sends_original(tmp_path, monkeypatch):
    tools, _, _ = setup(tmp_path)
    tools.book_formats.set(CHAT, "azw3")
    monkeypatch.setattr(books.shutil, "which", lambda name: None)
    res = asyncio.run(tools.run(ToolCall("1", "book", {"title": "Limit"}), CHAT))
    assert "Calibre" in res.final and "im Original" in res.final
    view = next(x for x in tools.take_outbox(CHAT) if isinstance(x, View))
    assert [n for _, n in view.arg] == ["limit.epub"]
    assert books.norm_format("für Kindle") == "azw3" and books.norm_format(".EPUB") == "epub"


def test_book_suggestions_with_buttons(tmp_path):
    tools, _, _ = setup(tmp_path)
    tools.book_formats.by_chat.clear()
    picks = [{"title": "Limit", "author": "Frank Schätzing", "why": "Großer Thriller"},
             {"title": "Der Schwarm", "author": "Frank Schätzing", "why": "Ozean statt Tunnel"}]
    asyncio.run(tools.run(ToolCall("1", "book_suggest", {"intro": "Endzeit und Spannung:", "books": picks}), CHAT))
    flow = next(x for x in tools.take_outbox(CHAT) if isinstance(x, Flow))
    assert "1. Limit – Frank Schätzing\n   Großer Thriller" in flow.step.text
    assert [v for _, v in flow.step.buttons] == ["s0", "s1", "no"]
    flow = asyncio.run(tools.choose(flow.id, "s0"))
    assert not flow.step.done and "welchem Format" in flow.step.text  # no edition question
    flow = asyncio.run(tools.choose(flow.id, "f:epub"))
    assert flow.step.done and "Hier ist „Limit" in flow.step.text
