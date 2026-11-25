"""SABnzbd, downloads view, users/limits, TMDB features and extending series (all offline)."""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from arrbot import downloads
from arrbot.arr import Radarr, Sonarr
from arrbot.config import ArrConfig
from arrbot.llm import ToolCall
from arrbot.sabnzbd import SABnzbd, SabSlot, eta_text, parse_timeleft
from arrbot.state import Store, utcnow
from arrbot.tmdb import TMDB, GermanInfo, german_summary
from arrbot.tools import DownloadsView, Flow, Offer, ToolRunner, _season_overview, _seasons_text, release_languages
from arrbot.tracker import Tracker
from arrbot.users import BLOCKED, PENDING, USER, UserStore

from .test_bot import FakeArr

TZ = ZoneInfo("Europe/Berlin")


class Library(FakeArr):
    """FakeArr plus library listing and indexer releases."""

    def __init__(self):
        super().__init__()
        self.releases: list[dict] = []
        self.deleted: list[tuple[str, dict]] = []

    def handler(self, req):
        p = req.url.path.removeprefix("/api/v3")
        if req.method == "GET" and p == "/movie":
            return httpx.Response(200, json=list(self.movies.values()))
        if req.method == "GET" and p == "/series":
            return httpx.Response(200, json=list(self.series.values()))
        if req.method == "GET" and p == "/release":
            return httpx.Response(200, json=self.releases)
        last = p.split("/")[-1]
        if p.startswith("/movie/") and last.isdigit():
            if req.method == "GET":
                return httpx.Response(200, json=self.movies[int(last)])
            if req.method == "PUT":
                self.movies[int(last)] = json.loads(req.content)
                return httpx.Response(202, json=self.movies[int(last)])
            if req.method == "DELETE":
                self.deleted.append((p, dict(req.url.params)))
                self.movies.pop(int(last))
                return httpx.Response(200)
        if req.method == "DELETE" and p.startswith(("/queue/", "/series/")):
            self.deleted.append((p, dict(req.url.params)))
            return httpx.Response(200)
        return super().handler(req)


def make(tmp_path):
    fr, fs = Library(), Library()
    radarr = Radarr(ArrConfig("http://r", "k"), transport=httpx.MockTransport(fr.handler))
    sonarr = Sonarr(ArrConfig("http://s", "k"), transport=httpx.MockTransport(fs.handler))
    return fr, fs, radarr, sonarr, Store(str(tmp_path))


def sab_with(slots: list[dict], paused=False, kbpersec="12800"):
    def handler(req):
        assert req.url.params["mode"] == "queue" and req.url.params["apikey"] == "sabkey"
        return httpx.Response(200, json={"queue": {"paused": paused, "kbpersec": kbpersec, "slots": slots}})

    return SABnzbd("http://sab:8080", "sabkey", transport=httpx.MockTransport(handler))


SLOT = {"nzo_id": "SABnzbd_nzo_abc", "filename": "Dune.Part.Two.2024.German.1080p", "status": "Downloading",
        "percentage": "62", "mb": "5120", "mbleft": "1945.6", "timeleft": "0:23:10", "index": 0}


# ---- SABnzbd -------------------------------------------------------------------
def test_sab_timeleft_and_eta():
    assert parse_timeleft("0:23:10") == timedelta(minutes=23, seconds=10)
    assert parse_timeleft("1:02:00:00") == timedelta(days=1, hours=2)
    assert parse_timeleft("0:00:00") is None and parse_timeleft("x") is None
    now = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)  # 21:00 in Berlin
    slot = SabSlot("a", "x", "Downloading", 50, 100, 50, timedelta(minutes=45), 0)
    assert eta_text(slot, now, TZ) == "fertig ca. 21:45 Uhr (noch 45 min)"
    slot.timeleft = timedelta(hours=4)
    assert eta_text(slot, now, TZ) == "fertig ca. morgen 01:00 Uhr (noch 4 h 00 min)"
    assert eta_text(slot, now, TZ, queue_paused=True) == "pausiert in SABnzbd"


def test_sab_queue_parsing():
    q = asyncio.run(sab_with([SLOT]).queue())
    s = q.slots["SABnzbd_nzo_abc"]
    assert (s.percent, s.mb_total, s.status) == (62, 5120.0, "Downloading")
    assert q.speed_text == "12.5 MB/s" and not q.paused


# ---- tracker with SABnzbd --------------------------------------------------------
def test_grab_message_has_finish_time_and_pending_follow_up(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    sent = []

    async def send(chat, text):
        sent.append((chat, text))

    slots: list[dict] = []
    sab = SABnzbd("http://sab", "k", transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"queue": {"paused": False, "kbpersec": "10240", "slots": slots}})))
    tracker = Tracker(radarr, sonarr, store, send, 30, 30, sab=sab, tz=TZ)
    store.track("radarr", 7, "Dune: Teil Zwei (2024)", 42)
    store.state.cursors["radarr"] = (utcnow() - timedelta(minutes=5)).isoformat()
    store.state.cursors["sonarr"] = utcnow().isoformat()
    fr.history = [{"id": 1, "movieId": 7, "eventType": "grabbed", "date": utcnow().isoformat(),
                   "downloadId": "SABnzbd_nzo_abc", "quality": {"quality": {"name": "Bluray-1080p"}}}]
    asyncio.run(tracker.poll())  # SABnzbd does not know the download yet
    assert sent[0][1].startswith("⬇️ Download gestartet: Dune: Teil Zwei (2024)") and "⏱" not in sent[0][1]
    assert store.state.tracked["radarr:7"].pending_downloads == [["SABnzbd_nzo_abc", 1]]

    slots.append(SLOT)
    asyncio.run(tracker.poll())
    assert "⏱ fertig ca." in sent[1][1] and "5,0 GB" in sent[1][1]
    assert store.state.tracked["radarr:7"].pending_downloads == []

    fr.history.append({"id": 2, "movieId": 7, "eventType": "downloadFolderImported", "date": utcnow().isoformat()})
    asyncio.run(tracker.poll())
    assert sent[-1][1].startswith("✅ Fertig")
    assert store.state.finished[-1]["title"] == "Dune: Teil Zwei (2024)" and store.state.finished[-1]["chat_ids"] == [42]


# ---- downloads overview -----------------------------------------------------------
def test_downloads_view_with_progress_bar_and_owner_filter(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    store.track("radarr", 7, "Dune: Teil Zwei (2024)", 42)
    store.track("sonarr", 3, "Severance", 99)
    store.state.tracked["radarr:7"].grabbed = True
    fr.queue = [{"id": 1, "movieId": 7, "downloadId": "SABnzbd_nzo_abc", "status": "downloading",
                 "size": 5 * 2**30, "sizeleft": 2 * 2**30, "movie": {"title": "Dune: Part Two", "year": 2024}}]
    fs.queue = [{"id": 2, "seriesId": 3, "downloadId": "nzo_s", "status": "queued", "size": 2**30, "sizeleft": 2**30,
                 "series": {"title": "Severance"}, "episode": {"seasonNumber": 1, "episodeNumber": e}} for e in (1, 2)]
    store.state.finished.append({"title": "Dune (2021)", "service": "radarr", "chat_ids": [42],
                                 "at": utcnow().isoformat()})
    sab_q = asyncio.run(sab_with([SLOT]).queue())
    snap = asyncio.run(downloads.collect(radarr, sonarr, store, sab_q, True, TZ))
    assert len(snap.items) == 2 and snap.items[1].episodes == ["S01E01", "S01E02"]  # season pack grouped

    mine = downloads.render(snap, 42, False, str, TZ)
    assert "Deine Downloads" in mine and "Dune: Part Two" in mine and "Severance" not in mine
    assert f"<code>{downloads.bar(62)}</code> 62 %" in mine and "⚡ 12.5 MB/s" in mine
    assert "⏱ fertig ca." in mine and "✅ Fertig" in mine and "Dune (2021)" in mine

    everything = downloads.render(snap, 42, True, lambda uid: {99: "Anna"}.get(uid, str(uid)), TZ)
    assert "Severance" in everything and "👤 Anna" in everything and "Warteschlange" in everything
    assert downloads.bar(0) == "░" * 10 and downloads.bar(100) == "█" * 10


def test_empty_downloads_view(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    snap = asyncio.run(downloads.collect(radarr, sonarr, store, None, False, TZ))
    assert "Gerade läuft nichts" in downloads.render(snap, 1, False, str, TZ)


def test_download_status_tool_shows_view_without_ai_text(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released")
    res = asyncio.run(tools.run(ToolCall("1", "download_status", {}), 5))
    assert res.final == "" and isinstance(tools.take_outbox(5)[0], DownloadsView)


# ---- users and limits ---------------------------------------------------------------
def test_users_access_flow_and_admin_protection(tmp_path):
    users = UserStore(str(tmp_path), admin_id=1, seed_users={2})
    assert users.is_admin(1) and users.is_allowed(2) and not users.is_allowed(3)
    assert users.request(3, "Anna") is True and users.request(3, "Anna") is False
    assert users.role(3) == PENDING
    users.set_role(3, USER)
    assert users.is_allowed(3)
    users.set_role(3, BLOCKED)
    assert not users.is_allowed(3)
    try:
        users.set_role(1, BLOCKED)
        raise AssertionError("admin must stay admin")
    except ValueError:
        pass
    again = UserStore(str(tmp_path), admin_id=1)
    assert again.role(3) == BLOCKED and again.name(3) == "Anna"


def test_limits_per_user_defaults_and_total(tmp_path):
    users = UserStore(str(tmp_path), admin_id=1, seed_users={2, 3})
    users.set_limit("standard", "tag", 2)
    users.set_limit(3, "tag", 5)
    for _ in range(2):
        assert users.blocked_by_limit(2) is None
        users.count_request(2)
    assert "2 Anfragen pro Tag" in users.blocked_by_limit(2)
    assert users.blocked_by_limit(3) is None  # own limit beats the default
    users.count_request(1)
    users.count_request(1)
    users.count_request(1)
    assert users.blocked_by_limit(1) is None  # defaults never apply to the admin

    users.set_limit("gesamt", "kosten", 0.01)
    users.add_usage(3, 1000, 0.02)
    assert "Gesamtbudget" in users.blocked_by_limit(1)
    users.set_limit("gesamt", "kosten", None)
    users.set_limit(3, "tokens", 500)
    assert "Tokens" in users.blocked_by_limit(3)

    reloaded = UserStore(str(tmp_path), admin_id=1)
    assert reloaded.defaults == {"tag": 2} and reloaded.users[3].usage.month_tokens == 1000
    assert reloaded.find("3") == 3


def test_old_users_file_format_is_migrated(tmp_path):
    (tmp_path / "users.json").write_text(json.dumps({"1": {"id": 1, "name": "Ich", "role": "admin", "since": "x"},
                                                     "5": {"id": 5, "name": "Bob", "role": "user", "since": "x"}}))
    users = UserStore(str(tmp_path), admin_id=1)
    assert users.is_allowed(5) and users.limits_for(5) == {}


# ---- extending series -------------------------------------------------------------------
def season(n, files, total):
    return {"seasonNumber": n, "monitored": n == 1,
            "statistics": {"episodeFileCount": files, "totalEpisodeCount": total}}


def test_existing_series_offers_missing_seasons(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    fs.series[3] = {"id": 3, "tvdbId": 371980, "title": "Severance", "status": "continuing",
                    "seasons": [season(0, 0, 2), season(1, 9, 9), season(2, 0, 10), season(3, 0, 10)]}
    tools = ToolRunner(radarr, sonarr, store, "released")
    tools._remember(5, tools_cand := __import__("arrbot.tools", fromlist=["Candidate"]).Candidate(
        "series", 371980, "Severance", 2022, [1, 2, 3], lib_id=3))
    res = asyncio.run(tools.run(ToolCall("1", "add_series", {"tvdb_id": 371980}), 5))
    assert res.final == ""
    flow = tools.take_outbox(5)[0]
    assert isinstance(flow, Flow) and flow.stage == "extend"
    assert "Du hast Staffel 1. Es fehlen Staffel 2, 3." in flow.step.text
    assert flow.step.buttons[0] == ("⬇️ Alle fehlenden laden (St. 2, 3)", "rest")

    flow = asyncio.run(tools.choose(flow.id, "rest"))
    assert flow.step.done and "Staffel 2, 3" in flow.step.text
    assert {s["seasonNumber"]: s["monitored"] for s in fs.series[3]["seasons"]} == {0: False, 1: True, 2: True, 3: True}
    assert fs.commands == [{"name": "SeasonSearch", "seriesId": 3, "seasonNumber": n} for n in (2, 3)]
    assert tools_cand.lib_id == 3


def test_complete_series_says_so(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    fs.series[3] = {"id": 3, "title": "Severance", "status": "ended", "seasons": [season(1, 9, 9), season(2, 10, 10)]}
    tools = ToolRunner(radarr, sonarr, store, "released")
    from arrbot.tools import Candidate
    tools._remember(5, Candidate("series", 371980, "Severance", 2022, [1, 2], lib_id=3))
    res = asyncio.run(tools.run(ToolCall("1", "add_series", {"tvdb_id": 371980}), 5))
    assert res.final == "👍 Severance ist komplett vorhanden (Staffel 1, 2)."


def test_season_helpers():
    assert _seasons_text([1, 2, 3, 5]) == "Staffel 1–3, 5"
    assert _seasons_text([2, 3], short=True) == "St. 2, 3"
    assert _season_overview({"seasons": [season(1, 9, 9), season(2, 3, 10)]}) == ([1], [2])
    rel = release_languages([{"title": "Film.2024.German.DL.1080p", "languages": []},
                             {"title": "Film.2024.1080p", "languages": [{"name": "English"}]},
                             {"title": "x", "languages": [{"name": "German"}]}])
    assert rel == {"releases": 3, "deutsch": 2}


# ---- TMDB ----------------------------------------------------------------------------------
def fake_tmdb(calls=None):
    def handler(req):
        p, q = req.url.path.removeprefix("/3"), req.url.params
        if calls is not None:
            calls.append((p, dict(q)))
        if p.startswith("/discover/movie"):
            return httpx.Response(200, json={"results": [
                {"id": 693134, "title": "Dune: Teil Zwei", "release_date": "2024-02-27", "vote_average": 8.2, "vote_count": 6000},
                {"id": 1, "title": "Alles steht Kopf 2", "release_date": "2024-06-11", "vote_average": 7.6, "vote_count": 5000},
                {"id": 2, "title": "Deadpool & Wolverine", "release_date": "2024-07-24", "vote_average": 7.7, "vote_count": 7000},
            ]})
        if p.endswith("/recommendations"):
            return httpx.Response(200, json={"results": [
                {"id": 438631, "title": "Dune", "release_date": "2021-09-15", "vote_average": 7.8, "vote_count": 12000},
                {"id": 335984, "title": "Blade Runner 2049", "release_date": "2017-10-04", "vote_average": 7.6, "vote_count": 13000},
            ]})
        if p.startswith("/movie/"):
            return httpx.Response(200, json={
                "title": "Der Film", "original_title": "The Movie", "original_language": "en",
                "release_dates": {"results": [{"iso_3166_1": "DE", "release_dates": [
                    {"type": 3, "release_date": "2026-10-01T00:00:00.000Z"},
                    {"type": 4, "release_date": "2027-01-15T00:00:00.000Z"},
                ]}]},
                "watch/providers": {"results": {"DE": {"rent": [{"provider_name": "Apple TV"}]}}},
            })
        return httpx.Response(404)

    return TMDB("abc", transport=httpx.MockTransport(handler))


def test_top_titles_list_and_download(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    fr.movies[1] = {"id": 1, "tmdbId": 693134, "title": "Dune: Part Two", "hasFile": True}
    calls = []
    tools = ToolRunner(radarr, sonarr, store, "released", tmdb=fake_tmdb(calls))
    res = asyncio.run(tools.run(ToolCall("1", "top_titles", {"kind": "movie", "year": 2024, "count": 3}), 5))
    assert res.final == ""
    offer = tools.take_outbox(5)[0]
    assert isinstance(offer, Offer) and offer.text.startswith("🏆 Top 3 Filme 2024 (meistgesehen):")
    assert "1. Dune: Teil Zwei (2024) ⭐ 8,2 ✅ schon da" in offer.text
    assert [c.title for c in offer.cands] == ["Alles steht Kopf 2", "Deadpool & Wolverine"]  # only new ones
    disc = [q for p, q in calls if p == "/discover/movie"][0]
    assert disc["primary_release_date.gte"] == "2024-01-01" and disc["primary_release_date.lte"] == "2024-12-31"

    res = asyncio.run(tools.run(ToolCall("2", "top_titles", {"kind": "movie", "year": 2025, "month": 10,
                                                             "count": 3, "download": True}), 5))
    assert "🏆 Top 3 Filme Oktober 2025" in res.final
    assert "👍 Dune: Teil Zwei (2024) – schon da" in res.final and res.final.count("– hinzugefügt") == 2
    assert len([c for c in fr.commands if c["name"] == "MoviesSearch"]) == 2
    disc = [q for p, q in calls if p == "/discover/movie"][-1]
    assert disc["primary_release_date.gte"] == "2025-10-01" and disc["primary_release_date.lte"] == "2025-10-31"


def test_suggestions_from_history_skip_owned(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    fr.movies[1] = {"id": 1, "tmdbId": 438631, "title": "Dune", "added": "2026-01-01"}
    store.record_request(5, "movie", "Arrival (2016)", 329865)
    tools = ToolRunner(radarr, sonarr, store, "released", tmdb=fake_tmdb())
    res = asyncio.run(tools.run(ToolCall("1", "suggest", {"kind": "movie"}), 5))
    assert res.final == ""
    offer = tools.take_outbox(5)[0]
    assert "weil du Arrival (2016)" in offer.text
    assert [c.title for c in offer.cands] == ["Blade Runner 2049"]  # Dune is already in the library
    assert offer.cands[0].label == "🎬 Blade Runner 2049 (2017) · ⭐ 7,6"


def test_german_info_and_summary(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released", tmdb=fake_tmdb())
    res = asyncio.run(tools.run(ToolCall("1", "german_info", {"kind": "movie", "id": 5}), 5))
    data = json.loads(res.content)
    assert data["de_title"] == "Der Film" and data["kino_de"] == "2026-10-01" and data["digital_de"] == "2027-01-15"
    info = GermanInfo("movie", 5, "Der Film", "The Movie", "en", True, cinema=date(2026, 10, 1), digital=date(2027, 1, 15))
    assert german_summary(info, date(2026, 11, 25)) == (
        "🇩🇪 Auf Deutsch zu Hause erst ab 15.01.2027 (digital), im Kino seit 01.10.2026."
    )
    info = GermanInfo("movie", 5, "Der Film", "The Movie", "en", True)
    assert "keine Veröffentlichung" in german_summary(info, date(2026, 11, 25))
    assert german_summary(GermanInfo("movie", 5, "Film", "Film", "de", True), date(2026, 11, 25)) is None


def test_added_movie_mentions_german_release(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released", tmdb=fake_tmdb())
    notices = []

    async def on_added(chat, text):
        notices.append((chat, text))

    tools.on_added = on_added
    res = asyncio.run(tools.run(ToolCall("1", "add_movie", {"tmdb_id": 693134}), 5))
    assert "hinzugefügt" in res.final and "🇩🇪" in res.final
    assert notices and notices[0][0] == 5
    assert store.state.requests[-1]["tmdb"] == 693134


def test_bot_builds(tmp_path, monkeypatch):
    from arrbot.bot import build_app
    from arrbot.config import load_config

    for k, v in {"TELEGRAM_BOT_TOKEN": "123:abc", "ADMIN_USER_ID": "1", "LLM_PROVIDER": "openai",
                 "LLM_API_KEY": "k", "RADARR_URL": "http://r", "RADARR_API_KEY": "k",
                 "SABNZBD_URL": "http://sab:8080", "SABNZBD_API_KEY": "k", "TMDB_API_KEY": "t",
                 "DATA_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    app = build_app(load_config())
    assert app.handlers


# ---- delete, pause, resume -------------------------------------------------------------------
def movie_in_library(fr, store, owner=5):
    fr.movies[7] = {"id": 7, "tmdbId": 693134, "title": "Dune: Part Two", "year": 2024, "monitored": True,
                    "hasFile": True, "sizeOnDisk": 8 * 2**30}
    store.record_request(owner, "movie", "Dune: Teil Zwei (2024)", 693134)
    store.track("radarr", 7, "Dune: Teil Zwei (2024)", owner)


def test_remove_asks_then_deletes_with_files_and_download(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    movie_in_library(fr, store)
    fr.queue = [{"id": 55, "movieId": 7, "downloadId": "x"}]
    tools = ToolRunner(radarr, sonarr, store, "released")
    res = asyncio.run(tools.run(ToolCall("1", "remove_title", {"kind": "movie", "lib_id": 7}), 5))
    assert res.final == "" and 7 in fr.movies  # nothing deleted before the button
    flow = tools.take_outbox(5)[0]
    assert "Es sind Dateien vorhanden (8,0 GB)" in flow.step.text
    assert [v for _, v in flow.step.buttons] == ["files", "keep", "no"]
    flow = asyncio.run(tools.choose(flow.id, "files"))
    assert flow.step.done and "gelöscht inkl. Dateien" in flow.step.text
    assert ("/queue/55", {"removeFromClient": "true", "blocklist": "false"}) in fr.deleted
    assert ("/movie/7", {"deleteFiles": "true", "addImportExclusion": "false"}) in fr.deleted
    assert "radarr:7" not in store.state.tracked


def test_remove_cancel_and_permissions(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    movie_in_library(fr, store, owner=5)
    tools = ToolRunner(radarr, sonarr, store, "released")
    res = asyncio.run(tools.run(ToolCall("1", "remove_title", {"kind": "movie", "lib_id": 7}), 6))
    assert res.is_error and "nicht angefragt" in res.content  # someone else's title
    store.record_request(6, "movie", "Dune: Teil Zwei (2024)", 693134)
    res = asyncio.run(tools.run(ToolCall("1", "remove_title", {"kind": "movie", "lib_id": 7}), 6))
    assert res.is_error and "auch andere" in res.content  # shared: only the admin may delete
    tools.is_admin = lambda uid: uid == 1
    asyncio.run(tools.run(ToolCall("1", "remove_title", {"kind": "movie", "lib_id": 7}), 1))
    flow = tools.take_outbox(1)[0]
    flow = asyncio.run(tools.choose(flow.id, "no"))
    assert flow.step.text == "👍 Dune: Part Two (2024) bleibt." and fr.deleted == []


def test_pause_three_days_then_tracker_resumes(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    movie_in_library(fr, store)
    tools = ToolRunner(radarr, sonarr, store, "released", tz=TZ)
    res = asyncio.run(tools.run(ToolCall("1", "pause_title", {"kind": "movie", "lib_id": 7, "days": 3}), 5))
    assert res.final.startswith("⏸ Suche nach Dune: Part Two (2024) pausiert. Ich suche am ")
    assert fr.movies[7]["monitored"] is False and "radarr:7" in store.state.paused

    sent = []

    async def send(chat, text, kb=None):
        sent.append(text)

    tracker = Tracker(radarr, sonarr, store, send, 30, 30)
    store.state.cursors.update(radarr=utcnow().isoformat(), sonarr=utcnow().isoformat())
    store.state.tracked["radarr:7"].added_at = (utcnow() - timedelta(hours=2)).isoformat()
    asyncio.run(tracker.poll())
    assert sent == []  # paused: no "nothing found" notice, no resume yet

    store.state.paused["radarr:7"]["until"] = (utcnow() - timedelta(minutes=1)).isoformat()
    asyncio.run(tracker.poll())
    assert sent == ["▶️ Die Pause ist vorbei: Ich suche jetzt wieder nach Dune: Part Two (2024)."]
    assert fr.movies[7]["monitored"] is True and {"name": "MoviesSearch", "movieIds": [7]} in fr.commands
    assert "radarr:7" not in store.state.paused


def test_pause_without_date_and_resume(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    movie_in_library(fr, store)
    tools = ToolRunner(radarr, sonarr, store, "released")
    res = asyncio.run(tools.run(ToolCall("1", "pause_title", {"kind": "movie", "lib_id": 7}), 5))
    assert "Sag „weiter mit Dune: Part Two“" in res.final and store.state.paused["radarr:7"]["until"] is None
    snap = asyncio.run(downloads.collect(radarr, sonarr, store, None, False, TZ))
    assert "⏸ Pausiert" in downloads.render(snap, 5, False, str, TZ)
    res = asyncio.run(tools.run(ToolCall("2", "resume_title", {"kind": "movie", "lib_id": 7}), 5))
    assert res.final.startswith("▶️ Suche nach Dune: Part Two (2024) läuft wieder")
    assert fr.movies[7]["monitored"] is True and fr.commands[-1] == {"name": "MoviesSearch", "movieIds": [7]}
