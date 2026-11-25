"""Tests for link identification, answer buttons and Telegram formatting."""

import asyncio
import json

import httpx
import pytest

from arrbot import fmt, urlinfo
from arrbot.llm import ToolCall
from arrbot.tools import ToolRunner
from tests.test_bot import make

LETTERBOXD = """<html><head>
<meta property="og:title" content="Project Hail Mary (2026)" />
<meta property="og:type" content="video.movie" />
<meta property="og:description" content="A lone astronaut must save the earth &amp; humanity." />
<script type="application/ld+json">{"@type":"Movie","name":"Project Hail Mary",
 "dateCreated":"2026-03-20","sameAs":["https://www.imdb.com/title/tt12042730/"]}</script>
</head><body><a href="https://www.themoviedb.org/movie/687163/">TMDB</a>
<a href="https://www.imdb.com/title/tt0816692/">Interstellar (related)</a></body></html>"""


def test_ids_from_urls():
    assert urlinfo.ids_from_text("https://www.imdb.com/de/title/tt0303461/?ref_=x") == {"imdb": "tt0303461"}
    assert urlinfo.ids_from_text("https://www.themoviedb.org/tv/12971-dragon-ball-z") == {"tmdb_tv": 12971}
    assert urlinfo.ids_from_text("https://thetvdb.com/?tab=series&id=81472") == {"tvdb": 81472}


def test_parse_page_prefers_structured_data():
    info = urlinfo.parse_page(LETTERBOXD, "https://letterboxd.com/film/project-hail-mary/")
    assert info["title"] == "Project Hail Mary" and info["year"] == 2026 and info["kind"] == "movie"
    assert info["ids"]["imdb"] == "tt12042730"  # from sameAs, not the related Interstellar link
    assert info["ids"]["tmdb_movie"] == 687163
    assert "&" in info["description"]


def test_identify_follows_redirects_and_keeps_url_ids():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/short":
            return httpx.Response(301, headers={"location": "https://letterboxd.com/film/phm/"})
        return httpx.Response(200, text=LETTERBOXD, headers={"content-type": "text/html; charset=utf-8"})

    info = asyncio.run(urlinfo.identify("letterboxd.com/short", httpx.MockTransport(handler), check_host=False))
    assert info["url"] == "https://letterboxd.com/film/phm/" and info["ids"]["tmdb_movie"] == 687163


def test_identify_falls_back_to_url_ids_when_page_blocks():
    transport = httpx.MockTransport(lambda req: httpx.Response(403))
    info = asyncio.run(urlinfo.identify("https://www.imdb.com/title/tt0111161/", transport, check_host=False))
    assert info["ids"] == {"imdb": "tt0111161"} and "page_error" in info


def test_local_network_links_are_refused():
    with pytest.raises(urlinfo.UrlError, match="lokale Netz"):
        asyncio.run(urlinfo.identify("http://127.0.0.1:7878/"))
    with pytest.raises(urlinfo.UrlError, match="http"):
        asyncio.run(urlinfo.identify("ftp://example.com/x"))


def test_open_url_and_buttons_tools(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released")
    tools.url_options = {
        "transport": httpx.MockTransport(lambda r: httpx.Response(200, text=LETTERBOXD)),
        "check_host": False,
    }
    res = asyncio.run(tools.run(ToolCall("u", "open_url", {"url": "https://letterboxd.com/film/x/"}), 1))
    assert json.loads(res.content)["ids"]["imdb"] == "tt12042730"

    long = "x" * 60
    res = asyncio.run(tools.run(ToolCall("b", "show_buttons", {"options": ["Alle Staffeln", " ", long]}), 1))
    assert not res.is_error
    assert tools.take_buttons(1) == ["Alle Staffeln", "x" * 40]
    assert tools.take_buttons(1) == []


def test_to_html_formats_and_escapes():
    out = fmt.to_html("Gefunden:\n1. **Tom & Jerry <2021>**\n- Punkt\n`code`")
    assert "<b>Tom &amp; Jerry &lt;2021&gt;</b>" in out
    assert "• Punkt" in out and "<code>code</code>" in out


def test_markdown_table_becomes_monospace():
    out = fmt.to_html("Status:\n| Titel | % |\n|---|---|\n| Dune | 45 |\n| The Office S01E02 | 3 |\nEnde")
    assert out.startswith("Status:\n<pre>") and out.endswith("</pre>\nEnde")
    pre = out[out.index("<pre>") + 5 : out.index("</pre>")]
    lines = pre.split("\n")
    assert lines[0].startswith("Titel") and set(lines[1]) == {"─"}
    assert lines[2].index("45") == lines[3].index("3")  # columns aligned


def test_chunks_keep_tables_together():
    text = "a" * 30 + "\n| x | y |\n| 1 | 2 |\n| 3 | 4 |\nend"
    parts = fmt.chunks(text, size=40)
    assert all("| x | y |" not in p or "| 3 | 4 |" in p for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")
