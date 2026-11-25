"""🌐 Website watch: only new lines cost tokens, titles become buttons (offline)."""

import asyncio

from arrbot import sitewatch, urlinfo
from arrbot.llm import ToolCall, Turn
from arrbot.profiles import ProfileStore
from arrbot.state import Store
from arrbot.tools import Offer, ToolRunner

from .test_extras import radarr

PAGE1 = """<html><head><title>x</title><script>var a=1;</script></head><body>
<nav>Start</nav><h1>Neu im Kino</h1><ul><li>Dune: Part Two (2024)</li><li>Wicked (2024)</li>
<li>Impressum</li><li>Kontakt</li><li>Datenschutz</li></ul></body></html>"""
PAGE2 = PAGE1.replace("<li>Impressum</li>", "<li>Nosferatu (2024)</li><li>Smile 2 (2024)</li><li>Impressum</li>")


class FakeAI:
    def __init__(self):
        self.seen = []

    async def complete(self, system, history, tools):
        self.seen.append(history[0].text)
        items = [{"title": t, "year": 2024, "kind": "movie"} for t in ("Nosferatu", "Smile 2")
                 if t in history[0].text]
        return Turn("assistant", tool_calls=[ToolCall("1", "titles", {"items": items})],
                    usage={"input": 120, "output": 30})


def test_page_lines_and_new_lines():
    lines = sitewatch.page_lines(PAGE1)
    assert "Dune: Part Two (2024)" in lines and not any("var a" in x for x in lines)
    site = sitewatch.Site(1, 2, "https://kino.example/neu", "all", sitewatch.digest(lines), lines, None, 0)
    assert sitewatch.new_lines(site, sitewatch.page_lines(PAGE2)) == ["Nosferatu (2024)", "Smile 2 (2024)"]


def test_unchanged_page_costs_nothing_new_titles_become_buttons(tmp_path, monkeypatch):
    pages = iter([PAGE1, PAGE1, PAGE2])

    async def fake_fetch(url, transport=None, check_host=True):
        return url, next(pages)

    monkeypatch.setattr(urlinfo, "fetch", fake_fetch)
    lookups = {
        "Nosferatu 2024": [{"title": "Nosferatu", "year": 2024, "tmdbId": 426063, "genres": ["Horror", "Fantasy"]}],
        "Smile 2 2024": [{"title": "Smile 2", "year": 2024, "tmdbId": 1100782, "genres": ["Horror"], "id": 5}],
    }
    _, r = radarr({("GET", "/movie/lookup"): lambda req: lookups.get(req.url.params["term"], [])})
    tools = ToolRunner(r, None, Store(str(tmp_path)), "released")
    tools.sites = sitewatch.SiteStore(str(tmp_path))
    tools.profiles = ProfileStore(str(tmp_path))
    ai = FakeAI()

    res = asyncio.run(tools.run(ToolCall("1", "watch_site", {"action": "add", "url": "kino.example/neu"}), 2))
    assert res.final.startswith("👀 Ich beobachte kino.example auf neue Filme und Serien")
    site = tools.sites.sites(2)[0]
    assert asyncio.run(tools.check_site(site, ai)) == {} and not ai.seen  # unchanged: no AI call

    usage = asyncio.run(tools.check_site(tools.sites.sites(2)[0], ai))
    assert usage == {"input": 120, "output": 30}
    assert ai.seen == ["Nosferatu (2024)\nSmile 2 (2024)"]  # only the new lines went to the AI
    offer = tools.take_outbox(2)[0]
    assert isinstance(offer, Offer) and offer.text == "🌐 Neu auf kino.example:" and offer.suggest
    assert [c.title for c in offer.cands] == ["Nosferatu"]  # Smile 2 is already in Radarr
    assert tools.sites.sites(2)[0].found == 1
    assert "kino.example – Filme und Serien · bisher 1 gefunden" in sitewatch.render_sites(tools.sites.sites(2))
