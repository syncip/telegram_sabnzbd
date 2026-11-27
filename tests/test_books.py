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
    assert flow.step.buttons[0] == ("1. Der Schwarm (2004)", "b0") and flow.step.buttons[-1][1] == "no"
    assert "1. Der Schwarm – Frank Schätzing (2004)" in flow.step.text
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
    assert "1. 📗 Limit – Frank Schätzing\n   Großer Thriller" in flow.step.text  # checked in Chaptarr: there
    assert "2. Der Schwarm – Frank Schätzing (2004)" in flow.step.text
    assert [v for _, v in flow.step.buttons] == ["s0", "s1", "no"]
    flow = asyncio.run(tools.choose(flow.id, "s0"))
    assert not flow.step.done and "welchem Format" in flow.step.text  # no edition question
    flow = asyncio.run(tools.choose(flow.id, "f:epub"))
    assert flow.step.done and "Hier ist „Limit" in flow.step.text


def test_shelf_mine_all_pages_and_send_again(tmp_path):
    tools, _, _ = setup(tmp_path)
    tools.book_history = books.BookHistory(str(tmp_path))
    lib = [{"id": 7, "title": "Limit", "author": {"authorName": "Frank Schätzing"}, "hasFiles": True,
            "added": "2026-11-27T10:00:00Z"},
           {"id": 8, "title": "Metro 2033", "authorTitle": "Dmitry Glukhovsky", "statistics": {"bookFileCount": 1},
            "added": "2026-11-27T16:12:00Z"},
           {"id": 9, "title": "Der Schwarm", "monitored": True}]
    lib += [{"id": 100 + i, "title": f"Band {i}", "hasFiles": True, "added": "2020-01-01"} for i in range(8)]

    async def library(media="ebook"):
        return lib

    tools.chaptarr.library = library
    # nothing of mine yet: all books, newest first, 8 per page
    asyncio.run(tools.run(ToolCall("1", "book_library", {"mine": True}), CHAT))
    flow = next(x for x in tools.take_outbox(CHAT) if isinstance(x, Flow))
    t = flow.step.text
    assert "noch keine Bücher" in t and "Alle Bücher auf dem Server (11) · Seite 1/2" in t
    assert t.index("1. 📗 Metro 2033") < t.index("2. 📗 Limit")
    assert flow.step.buttons[:4] == [("📥 1", "n0"), ("📥 2", "n1"), ("📥 3", "n2"), ("📥 4", "n3")]
    assert ("Weiter ▶️", "pg:1") in flow.step.buttons and flow.step.rows[:2] == [4, 4]
    flow = asyncio.run(tools.choose(flow.id, "pg:1"))
    assert "Seite 2/2" in flow.step.text and "11. 🔎 Der Schwarm – wird gesucht" in flow.step.text
    assert ("📥 11", "n10") not in flow.step.buttons  # nothing to send yet
    flow = asyncio.run(tools.choose(flow.id, "pg:0"))
    flow = asyncio.run(tools.choose(flow.id, "n1"))  # Limit: sent, and the list stays for the next one
    assert not flow.step.done and flow.step.text.startswith("📚 Hier ist „Limit") and "Seite 1/2" in flow.step.text
    view = next(x for x in tools.take_outbox(CHAT) if isinstance(x, View))
    assert [n for _, n in view.arg] == ["limit.epub"]
    flow = asyncio.run(tools.choose(flow.id, "mine"))  # now Limit is mine
    assert "📚 Deine Bücher (1)" in flow.step.text and "1. 📗 Limit – Frank Schätzing" in flow.step.text
    assert asyncio.run(tools.choose(flow.id, "no")).step.done
    res = asyncio.run(tools.run(ToolCall("2", "book_library", {"query": "king"}), CHAT))
    assert "keine Bücher" in res.final


def test_check_tells_whether_books_are_readable(tmp_path):
    tools, _, _ = setup(tmp_path)

    async def library(media="ebook"):
        return [{"id": 7, "title": "Limit", "hasFiles": True}]

    tools.chaptarr.library = library
    ok = asyncio.run(tools.book_access())
    assert "lesbar ✅" in ok[0] and ok[1].startswith("🔄 Umwandeln (Calibre)")
    tools.book_paths = None
    bad = asyncio.run(tools.book_access())
    assert "nicht lesbar" in bad[0] and "/books/Frank/" in bad[0] and "BOOK_PATHS" in bad[0]


def test_editions_merged_and_long_titles_readable():
    long = "Schraubenverbindungen: Grundlagen, Berechnung, Eigenschaften, Handhabung"
    a = {"author": {"authorName": "Hubert Wiegand"}}
    hits = [dict(a, title=long, releaseDate="2007-01-01"), dict(a, title=long + " ", releaseDate="2013-05-01"),
            dict(a, title="Schraubenverbindungen: Grundlagen - Funktion - Berechnung", releaseDate="2010-01-01"),
            dict(a, title=long, releaseDate="1988-01-01", id=4)]
    out = books.dedupe(hits)
    assert len(out) == 2 and out[0].get("id") == 4  # the one in the library wins
    assert books.button(0, out[0]) == "📗 1. Schraubenverbindungen: Grundlagen… (1988)"
    assert long in books.numbered(out)[0]
    assert books.dedupe(hits[:2])[0]["releaseDate"] == "2013-05-01"  # else the newest printing


def test_metadata_still_prepared_then_found(tmp_path):
    tools, state, calls = setup(tmp_path)
    real = tools.chaptarr._req
    prepared = {"done": False}

    async def req(method, path, **kw):
        if path == "/book/import":
            from arrbot.arr import ArrError
            raise ArrError("Chaptarr POST /book/import -> HTTP 400: Failed to import book: The author and requested "
                           "book are being prepared by the metadata server. Chaptarr saved your request")
        if path == "/book" and method == "GET" and prepared["done"]:
            return [{"id": 11, "title": "Der Schwarm"}]
        return await real(method, path, **kw)

    tools.chaptarr._req = req
    asyncio.run(tools.run(ToolCall("1", "book", {"title": "Der Schwarm"}), CHAT))
    flow = next(x for x in tools.take_outbox(CHAT) if isinstance(x, Flow))
    flow = asyncio.run(tools.choose(flow.id, "b0"))
    assert flow.step.done and "bereitet Chaptarr gerade noch vor" in flow.step.text and "❌" not in flow.step.text
    assert books.PendingBooks(str(tmp_path)).items[0].book_id == 0  # survives a restart
    assert asyncio.run(tools.book_deliveries()) == []  # still preparing
    prepared["done"] = True
    assert asyncio.run(tools.book_deliveries()) == []  # in the library now: searched, no file yet
    assert tools.books.items[0].book_id == 11
    assert any(p == "/api/v1/command" for _, p, _ in calls)
    state["file"] = True
    (chat, text, files), = asyncio.run(tools.book_deliveries())
    assert "ist da" in text and [n for _, n in files] == ["schwarm.epub"]


def test_suggestions_checked_mine_left_out_recent_filtered(tmp_path):
    tools, _, _ = setup(tmp_path)
    tools.book_history = books.BookHistory(str(tmp_path))
    tools.book_history.add(CHAT, {"title": "Limit", "author": {"authorName": "Frank Schätzing"}})
    picks = [{"title": "Limit", "author": "Frank Schätzing", "why": "a"},
             {"title": "Erfundenes Buch", "author": "Niemand", "why": "b"},
             {"title": "Der Schwarm", "author": "Frank Schätzing", "why": "c"}]
    asyncio.run(tools.run(ToolCall("1", "book_suggest", {"books": picks}), CHAT))
    flow = next(x for x in tools.take_outbox(CHAT) if isinstance(x, Flow))
    assert "1. Der Schwarm" in flow.step.text and "Limit" not in flow.step.text
    assert "(2 weitere habe ich weggelassen: gibt es so nicht, hast du schon.)" in flow.step.text
    res = asyncio.run(tools.run(ToolCall("2", "book_suggest", {"books": picks, "recent": True}), CHAT))
    assert "nicht neu genug" in res.final  # Der Schwarm is from 2004
    ctx = tools.book_context(CHAT, "empfiehl mir ein Buch")
    assert ctx.startswith("[Bücher des Nutzers") and "Limit – Frank Schätzing" in ctx
    assert tools.book_context(CHAT, "lad Dune 2") == ""  # not for films: saves tokens


def test_book_charts_from_open_library():
    import httpx as hx

    from arrbot import booklists

    def handler(req):
        if req.url.path == "/search.json":
            assert "subject:crime" in req.url.params["q"] and "language:ger" in req.url.params["q"]
            return hx.Response(200, json={"docs": [
                {"title": "Neu", "author_name": ["A"], "first_publish_year": 2026},
                {"title": "Alt", "author_name": ["B"], "first_publish_year": 1999}]})
        return hx.Response(200, json={"works": [{"title": "Trend", "author_name": ["C"], "language": ["ger"]},
                                                {"title": "Nur Englisch", "language": ["eng"]}]})

    t = hx.MockTransport(handler)
    assert [b["title"] for b in asyncio.run(booklists.new_books("krimi", transport=t))] == ["Neu"]
    assert [b["title"] for b in asyncio.run(booklists.trending(transport=t))] == ["Trend"]


def test_downloaded_book_import_nudged_and_blocked_told(tmp_path):
    tools, _, _ = setup(tmp_path)
    tools.books.add(11, CHAT, "Der Schwarm")
    sent = []
    queue = [{"bookId": 11, "status": "completed", "trackedDownloadState": "importBlocked",
              "statusMessages": [{"messages": ["Not a valid ebook"]}]}]
    real = tools.chaptarr._req

    async def req(method, path, **kw):
        if path == "/queue":
            return {"records": queue}
        if path == "/command":
            sent.append(kw.get("json"))
            return {}
        return await real(method, path, **kw)

    tools.chaptarr._req = req
    (chat, text, files), = asyncio.run(tools.book_deliveries())
    assert "kann es nicht übernehmen: Not a valid ebook" in text and files == []
    assert sent == [{"name": "RefreshMonitoredDownloads"}]
    assert asyncio.run(tools.book_deliveries()) == []  # told once


def test_format_guessed_by_ai_is_ignored_and_other_format_offered(tmp_path):
    tools, _, _ = setup(tmp_path)
    tools.book_formats.by_chat.clear()
    tools.user_text[CHAT] = "schick mir Limit"  # no format in the user's words
    asyncio.run(tools.run(ToolCall("1", "book", {"title": "Limit", "format": "epub"}), CHAT))
    flow = next(x for x in tools.take_outbox(CHAT) if isinstance(x, Flow))
    assert "welchem Format" in flow.step.text  # asked, the AI's "epub" did not count
    flow = asyncio.run(tools.choose(flow.id, "f:epub"))
    assert not any(isinstance(x, Flow) for x in tools.take_outbox(CHAT))  # just asked: no second question
    # next time: sent right away, with one tap for another format
    tools.user_text[CHAT] = "schick mir Limit nochmal"
    asyncio.run(tools.run(ToolCall("2", "book", {"title": "Limit"}), CHAT))
    out = tools.take_outbox(CHAT)
    offer = next(x for x in out if isinstance(x, Flow))
    assert "Als EPUB geschickt" in offer.step.text and ("📄 PDF", "f:pdf") in offer.step.buttons
    assert ("📱 EPUB – Handy, Tolino, Kobo, Apple Books", "f:epub") not in offer.step.buttons
    done = asyncio.run(tools.choose(offer.id, "f:pdf"))
    assert done.step.done and "Hier ist" in done.step.text and tools.book_formats.get(CHAT) == "pdf"
    out = tools.take_outbox(CHAT)
    assert [n for _, n in next(x for x in out if isinstance(x, View)).arg] == ["limit.pdf"]
    assert not any(isinstance(x, Flow) for x in out)  # no offer after the offer
    # "als PDF" in the user's words counts
    tools.user_text[CHAT] = "schick mir Limit für den Kindle"
    asyncio.run(tools.run(ToolCall("3", "book", {"title": "Limit", "format": "azw3"}), CHAT))
    assert tools.book_formats.get(CHAT) == "azw3"


def test_shelf_rescan_drops_deleted_and_reset(tmp_path):
    tools, _, _ = setup(tmp_path)
    tools.book_history = books.BookHistory(str(tmp_path))
    for t in ("Weg", "Datei weg", "Limit"):
        tools.book_history.add(CHAT, {"title": t})
    commands = []
    lib = [{"id": 7, "title": "Limit", "hasFiles": True}, {"id": 8, "title": "Datei weg", "hasFiles": True}]
    real = tools.chaptarr._req

    async def req(method, path, **kw):
        if path == "/command":
            commands.append(kw["json"]["name"])
            return {}
        if path == "/book" and method == "GET" and "titleSlug" not in (kw.get("params") or {}):
            return lib
        if path == "/bookfile" and kw["params"]["bookId"] == 8:
            return [{"path": "/books/Frank/geloescht.epub"}]  # Chaptarr still thinks so, the disk says no
        return await real(method, path, **kw)

    tools.chaptarr._req = req
    tools.book_history.by_chat[str(CHAT)][1]["id"] = 8  # "Datei weg"
    tools.book_history.by_chat[str(CHAT)][0]["id"] = 7  # "Limit"
    asyncio.run(tools.run(ToolCall("1", "book_library", {"mine": True}), CHAT))
    flow = next(x for x in tools.take_outbox(CHAT) if isinstance(x, Flow))
    assert ("🔄 Neu prüfen", "rescan") in flow.step.buttons and ("🧹 Liste leeren", "reset") in flow.step.buttons
    flow = asyncio.run(tools.choose(flow.id, "rescan"))
    assert "RescanFolders" in commands
    assert "2 nicht mehr da" in flow.step.text and "Weg" in flow.step.text and "Datei weg" in flow.step.text
    assert "📚 Deine Bücher (1)" in flow.step.text and [e["title"] for e in tools.book_history.of(CHAT)] == ["Limit"]
    flow = asyncio.run(tools.choose(flow.id, "reset"))
    assert "leeren?" in flow.step.text and ("↩️ Zurück", "back") in flow.step.buttons
    flow = asyncio.run(tools.choose(flow.id, "reset!"))
    assert tools.book_history.of(CHAT) == [] and "Deine Liste ist leer" in flow.step.text
    assert "Alle Bücher auf dem Server" in flow.step.text


def test_book_sent_right_after_sabnzbd_finished(tmp_path):
    from types import SimpleNamespace

    tools, state, _ = setup(tmp_path)
    tools.books.add(11, CHAT, "Der Schwarm")
    slots = {"SABnzbd_nzo_1": object()}

    class Sab:
        async def queue(self):
            return SimpleNamespace(slots=slots)

    tools.sab = Sab()
    commands = []
    real = tools.chaptarr._req

    async def req(method, path, **kw):
        if path == "/queue":
            return {"records": [{"bookId": 11, "downloadId": "SABnzbd_nzo_1", "trackedDownloadState": "downloading"}]}
        if path == "/command":
            commands.append(kw["json"]["name"])
            return {}
        return await real(method, path, **kw)

    tools.chaptarr._req = req
    assert asyncio.run(tools.book_fast()) == []  # nothing followed yet: no calls at all
    asyncio.run(tools.book_deliveries())  # the normal round sees the download
    assert asyncio.run(tools.book_fast()) == [] and commands == []  # still downloading
    slots.clear()  # SABnzbd done
    assert asyncio.run(tools.book_fast()) == []  # file not imported yet, but Chaptarr was nudged
    assert commands == ["RefreshMonitoredDownloads"]
    state["file"] = True  # Chaptarr imported it
    (chat, text, files), = asyncio.run(tools.book_fast())
    assert "ist da" in text and [n for _, n in files] == ["schwarm.epub"]
    assert tools._follow() == ({}, {})  # done: the fast job is idle again
