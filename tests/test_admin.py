"""Admin features: limits by text, natural-language admin tools, statistics, API check, downloads view details."""

import asyncio

import httpx

from arrbot import downloads, health, stats
from arrbot.arr import Radarr, Sonarr
from arrbot.config import ArrConfig
from arrbot.llm import LLMProvider, ToolCall, Turn
from arrbot.state import utcnow
from arrbot.tmdb import TMDB
from arrbot.tools import ToolRunner
from arrbot.users import Usage, UserStore

from .test_features import TZ, fake_tmdb, make


def test_limit_standard_and_total_do_not_crash(tmp_path):
    users = UserStore(str(tmp_path), admin_id=1, seed_users={2})
    users.touch_name(2, "Anna")
    assert users.apply_limit("standard", "tag", "50") == "👍 Für alle Nutzer (Standard): 50 Anfragen pro Tag."
    assert users.apply_limit("gesamt", "kosten", "1,5") == "👍 Für den ganzen Bot: 1,50 $ pro Monat."
    assert users.apply_limit("Anna", "monat", "aus") == "👍 Für Anna: Anfragen pro Monat aus."
    for bad in (("Bob", "tag", "5"), ("standard", "woche", "5"), ("standard", "tag", "viel")):
        try:
            users.apply_limit(*bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert users.defaults == {"tag": 50.0} and users.total_limits == {"kosten": 1.5}


def test_admin_tools_only_for_admin(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    users = UserStore(str(tmp_path), admin_id=1, seed_users={2})
    users.touch_name(2, "Anna")
    tools = ToolRunner(radarr, sonarr, store, "released")
    tools.users, tools.is_admin = users, users.is_admin

    def names(uid):
        return {s.name for s in tools.specs_for(uid)}

    assert "admin_stats" in names(1) and "admin_stats" not in names(2)
    call = ToolCall("1", "admin_set_limit", {"who": "Anna", "kind": "tag", "value": "5"})
    res = asyncio.run(tools.run(call, 2))
    assert res.is_error and res.content == "Nur für Admins."
    res = asyncio.run(tools.run(call, 1))
    assert res.final == "👍 Für Anna: 5 Anfragen pro Tag."
    res = asyncio.run(tools.run(ToolCall("2", "admin_user", {"who": "Anna", "action": "sperren"}), 1))
    assert res.final == "🚫 Anna ist gesperrt." and not users.is_allowed(2)
    asyncio.run(tools.run(ToolCall("3", "admin_stats", {}), 1))
    assert tools.take_outbox(1)[0].kind == "stats"


def test_statistics_view(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    users = UserStore(str(tmp_path), admin_id=1, seed_users={2})
    users.touch_name(1, "Martin")
    users.touch_name(2, "Anna")
    for uid, tokens in ((1, 3000), (1, 3000), (2, 2000)):
        users.count_request(uid)
        users.add_usage(uid, tokens, 0.0005)
    users.set_limit("gesamt", "kosten", 1.0)
    store.record_request(2, "movie", "Dune (2021)", 438631)
    text = stats.render(users, store, "gpt-4o-mini", TZ)
    assert "👥 Nutzer: 2</b> (1 Admin, 1 Nutzer)" in text
    assert "Monat: 3 Anfragen · 3 KI-Aufrufe" in text and "8.000 Tokens" in text
    assert "Budget: 0,0015 $ von 1,00 $" in text
    assert text.index("Martin") < text.index("Anna")  # sorted by tokens
    assert "75 % der Tokens" in text and "Titel: 1 diesen Monat, 1 gesamt" in text


def test_usage_month_rollover_keeps_history():
    u = Usage(month="2026-10", month_requests=80, month_tokens=250000, month_cost=0.05, month_calls=170)
    u.roll()
    assert u.months["2026-10"] == {"requests": 80, "tokens": 250000, "cost": 0.05, "calls": 170}
    assert u.month_requests == 0


def test_top_titles_without_tmdb_says_what_is_missing(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released")  # no TMDB key
    assert "top_titles" in {s.name for s in tools.specs}  # always offered, so the AI never improvises
    res = asyncio.run(tools.run(ToolCall("1", "top_titles", {"kind": "movie", "year": 2025, "month": 12}), 5))
    assert "TMDB_API_KEY" in res.final and not res.is_error


def test_top_list_add_all_button(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    fr.movies[1] = {"id": 1, "tmdbId": 693134, "title": "Dune: Part Two", "hasFile": True}
    tools = ToolRunner(radarr, sonarr, store, "released", tmdb=fake_tmdb())
    asyncio.run(tools.run(ToolCall("1", "top_titles", {"kind": "movie", "year": 2024, "count": 3}), 5))
    offer = tools.take_outbox(5)[0]
    assert offer.bulk and len(offer.cands) == 2
    assert offer.text.endswith("Tippe einen Titel an, um ihn hinzuzufügen. Was schon da ist, hat keinen Button.")
    text = asyncio.run(tools.add_all(offer.id))
    assert text.startswith("✅ Alles steht Kopf 2 (2024)\n✅ Deadpool & Wolverine (2024)")
    assert len([c for c in fr.commands if c["name"] == "MoviesSearch"]) == 2
    assert asyncio.run(tools.add_all(offer.id)) is None  # buttons are single-use


def test_tmdb_error_is_shown_not_paraphrased(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    bad = TMDB("wrong", transport=httpx.MockTransport(lambda req: httpx.Response(401)))
    tools = ToolRunner(radarr, sonarr, store, "released", tmdb=bad)
    res = asyncio.run(tools.run(ToolCall("1", "top_titles", {"kind": "movie", "year": 2025}), 5))
    assert res.final == "⚠️ TMDB lehnt den API-Key ab (TMDB_API_KEY prüfen)."


def test_problems_grouped_in_german_and_finished_hidden(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    msg = ("Found matching series via grab history, but release was matched to series by ID. "
           "Automatic import is not possible. See the FAQ for details.")
    fs.queue = [{"id": e, "seriesId": 9, "downloadId": f"d{e}", "status": "completed", "size": 2e8, "sizeleft": 0,
                 "trackedDownloadStatus": "warning", "trackedDownloadState": "importPending",
                 "statusMessages": [{"messages": [msg]}],
                 "series": {"title": "Avatar: The Last Airbender (2024)"},
                 "episode": {"seasonNumber": 2, "episodeNumber": e}} for e in (3, 6, 7)]
    store.state.finished.append({"title": "Dune (2021)", "service": "radarr", "chat_ids": [1],
                                 "at": utcnow().isoformat()})
    snap = asyncio.run(downloads.collect(radarr, sonarr, store, None, False, TZ))
    text = downloads.render(snap, 1, True, str, TZ, show_finished=False)
    assert "Läuft gerade" not in text and text.count("Avatar") == 1
    assert "⚠️ Braucht Eingriff" in text and "S02E03, S02E06, S02E07" in text
    assert "Release passt nur per ID, nicht per Name" in text and "Sonarr → Aktivität" in text
    assert "Dune (2021)" not in text and "außerhalb des Bots" not in text
    assert "Dune (2021)" in downloads.render(snap, 1, True, str, TZ, show_finished=True)


def test_api_check(tmp_path):
    class OkLLM(LLMProvider):
        async def complete(self, system, history, tools):
            return Turn("assistant", text="ok", usage={"input": 50, "output": 1})

    class Me:
        username = "arrbot"

    class Bot:
        async def get_me(self):
            return Me()

    fr, fs, radarr, sonarr, store = make(tmp_path)

    def with_status(req):
        if req.url.path.endswith("/system/status"):
            return httpx.Response(200, json={"version": "5.14.0"})
        return fr.handler(req)

    radarr = Radarr(ArrConfig("http://r", "k"), transport=httpx.MockTransport(with_status))
    broken = Sonarr(ArrConfig("http://s", "k"), transport=httpx.MockTransport(lambda req: httpx.Response(401)))
    checks = asyncio.run(health.run_checks(bot=Bot(), provider=OkLLM(), model="gpt-4o-mini", radarr=radarr,
                                           sonarr=broken, sab=None, tmdb=fake_tmdb()))
    by = {c.name: c for c in checks}
    assert by["Telegram"].ok and by["Telegram"].detail == "@arrbot"
    assert by["KI"].ok and "51 Tokens" in by["KI"].detail
    assert by["Radarr"].ok and by["Radarr"].detail == "Version 5.14.0, 2 Qualitätsprofile"
    assert by["Sonarr"].ok is False and by["Sonarr"].detail == "API-Key falsch (401)"
    assert by["SABnzbd"].ok is None and by["TMDB"].ok
    text = health.render(checks)
    assert text.startswith("🩺 <b>API-Check</b> – 1 Problem ❌") and "⚪ <b>SABnzbd</b>" in text
