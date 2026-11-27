"""Media manager (1.14): delete on command with preview, series scope question, protection, duplicates."""

import asyncio
from datetime import datetime, timedelta, timezone

from arrbot import dupes, mediamgr
from arrbot.cleanup import RuleStore
from arrbot.llm import ToolCall
from arrbot.media import Report, Title
from arrbot.plex import Account
from arrbot.state import Store
from arrbot.tools import Flow, ToolRunner

NOW = datetime.now(timezone.utc)
ADMIN, ANNA = 1, 2
GB = 2**30


def title(kind, i, name, size_gb, days_ago, **kw):
    t = Title(kind, i, name, 2020, size_gb * GB, NOW - timedelta(days=days_ago), True, 1, 1,
              kw.pop("ended", True), kw.pop("ext", i))
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def report():
    return Report([
        title("movie", 1, "Gesehen", 10, 400, complete_by=[ADMIN], started_by=[ADMIN],
              viewed_by={ADMIN: NOW - timedelta(days=50)}),
        title("movie", 2, "Alt ungesehen", 20, 300),
        title("movie", 3, "Neu ungesehen", 5, 10),
        title("movie", 4, "Geschützt", 30, 300),
        title("movie", 5, "Anna schaut", 8, 300, started_by=[ANNA], viewed_by={ANNA: NOW - timedelta(days=2)}),
        title("movie", 6, "Anna will", 8, 300, ext=66),
        title("series", 7, "Dark", 40, 500, started_by=[ADMIN], complete_by=[ADMIN], plex_keys={ADMIN: "70"},
              viewed_by={ADMIN: NOW - timedelta(days=90)}),
    ], [ADMIN, ANNA], True)


def test_select_watched_unwatched_and_protection():
    r = report()
    hits, prot = mediamgr.select(r, mediamgr.Query("movie", watched=True), ADMIN, set(), {})
    assert [t.title for t in hits] == ["Gesehen"]
    hits, prot = mediamgr.select(r, mediamgr.Query("movie", watched=False, older_days=180), ADMIN,
                                 {("radarr", 4)}, {("movie", 66): ANNA}, lambda u: "Anna")
    assert [t.title for t in hits] == ["Alt ungesehen"]  # "Neu ungesehen" only 10 days old
    assert dict(prot) == {"Geschützt (2020)": "🛡 behalten", "Anna schaut (2020)": "👀 Anna schaut es gerade",
                          "Anna will (2020)": "🆕 Anna hat es neu angefragt"}
    assert mediamgr.select(r, mediamgr.Query("all", largest=1, min_gb=1), ADMIN, set(), {})[0][0].title == "Dark"


class FakeRadarr:
    def __init__(self):
        self.deleted = []

    async def delete(self, i, delete_files):
        self.deleted.append((i, delete_files))


class FakeSonarr(FakeRadarr):
    def __init__(self):
        super().__init__()
        self.files, self.unmonitored = [], []

    async def episodes(self, sid):
        def ep(i, s, e, size=GB):
            return {"id": i, "seasonNumber": s, "episodeNumber": e, "hasFile": True, "episodeFileId": 100 + i,
                    "episodeFile": {"size": size, "dateAdded": (NOW - timedelta(days=400)).isoformat()}}
        return [ep(1, 1, 1), ep(2, 1, 2), ep(3, 2, 1), ep(4, 2, 2)]

    async def delete_episode_files(self, ids):
        self.files += ids

    async def set_episodes_monitored(self, ids, on):
        self.unmonitored += [] if on else ids


class FakePlex:
    async def episodes(self, acc, key):
        # season 1 completely seen, season 2 only the first episode
        return [{"parentIndex": 1, "index": 1, "viewCount": 1, "lastViewedAt": 1},
                {"parentIndex": 1, "index": 2, "viewCount": 2, "lastViewedAt": 1},
                {"parentIndex": 2, "index": 1, "viewCount": 1, "lastViewedAt": 1}]


class FakeUsers:
    def is_allowed(self, uid):
        return True

    def name(self, uid):
        return "Anna" if uid == ANNA else "Admin"


def runner(tmp_path):
    radarr, sonarr = FakeRadarr(), FakeSonarr()
    tools = ToolRunner(radarr, sonarr, Store(str(tmp_path)), "released")
    tools.cleanup = RuleStore(str(tmp_path))
    tools.plex = FakePlex()
    tools.users = FakeUsers()
    tools.is_admin = lambda uid: uid == ADMIN
    acc = Account(ADMIN, "t", "admin", "http://p", "s", "m", "n", "x")

    async def rep():
        return report()

    tools._media_report = rep
    tools._plex_of = lambda chat: acc
    return tools, radarr, sonarr


def flow_of(tools, chat):
    return next(x for x in tools.take_outbox(chat) if isinstance(x, Flow))


def test_delete_watched_movies_after_confirmation(tmp_path):
    tools, radarr, _ = runner(tmp_path)
    res = asyncio.run(tools.run(ToolCall("1", "admin_delete_media", {"kind": "movie", "watched": True}), ADMIN))
    assert res.final == ""
    flow = flow_of(tools, ADMIN)
    assert "🗑 1 Eintrag · zusammen 10,0 GB" in flow.step.text and radarr.deleted == []  # preview first
    assert "nicht gesperrt" in flow.step.text
    flow = asyncio.run(tools.choose(flow.id, "all"))
    assert radarr.deleted == [(1, True)] and flow.step.done and "1 · 10,0 GB frei" in flow.step.text


def test_series_asks_for_scope_then_deletes_seen_seasons(tmp_path):
    tools, _, sonarr = runner(tmp_path)
    asyncio.run(tools.run(ToolCall("1", "admin_delete_media", {"kind": "series", "watched": True}), ADMIN))
    flow = flow_of(tools, ADMIN)
    assert "Was soll bei Serien weg?" in flow.step.text
    assert [v for _, v in flow.step.buttons][:3] == ["sc:episodes", "sc:seasons", "sc:series"]
    flow = asyncio.run(tools.choose(flow.id, "sc:seasons"))
    assert "Dark – Staffel 1 (2 Folgen) · 2,00 GB" in flow.step.text  # season 2 not fully seen
    flow = asyncio.run(tools.choose(flow.id, "one"))
    assert flow.step.text.startswith("🗂 1/1:") and "nicht mehr überwacht" in flow.step.text
    flow = asyncio.run(tools.choose(flow.id, "del"))
    assert sonarr.files == [101, 102] and sonarr.unmonitored == [1, 2] and sonarr.deleted == []
    assert flow.step.done


def test_episodes_scope_and_keep(tmp_path):
    tools, _, sonarr = runner(tmp_path)
    asyncio.run(tools.run(ToolCall("1", "admin_delete_media",
                                   {"kind": "series", "watched": True, "scope": "episodes"}), ADMIN))
    flow = flow_of(tools, ADMIN)
    assert "Dark: 3 Folgen (S01E01–S02E01)" in flow.step.text
    flow = asyncio.run(tools.choose(flow.id, "one"))
    flow = asyncio.run(tools.choose(flow.id, "keep"))
    assert flow.step.done and "🛡 Nie löschen: Dark" in flow.step.text and sonarr.files == []
    assert ("sonarr", 7) in tools.cleanup.kept()


def test_everything_is_never_a_rule(tmp_path):
    tools, radarr, _ = runner(tmp_path)
    res = asyncio.run(tools.run(ToolCall("1", "admin_delete_media", {"kind": "movie"}), ADMIN))
    assert res.is_error and "nachfragen" in res.content and radarr.deleted == []


def media(i, res, dur_min, langs, size_gb, bitrate=5000):
    return {"id": i, "duration": dur_min * 60000, "videoResolution": res, "bitrate": bitrate,
            "Part": [{"file": f"/m/{i}.mkv", "size": size_gb * GB,
                      "Stream": [{"streamType": 2, "languageCode": x} for x in langs]}]}


def test_duplicates_keep_preferred_language_then_resolution():
    metas = [
        {"type": "movie", "ratingKey": "10", "title": "Dune", "year": 2021, "Guid": [{"id": "tmdb://438631"}],
         "Media": [media(1, "4k", 155, ["eng"], 60), media(2, "1080", 156, ["ger", "eng"], 15),
                   media(3, "1080", 140, ["ger"], 12)]},
        {"type": "movie", "ratingKey": "11", "title": "Dune", "year": 2021, "Guid": [{"id": "tmdb://438631"}],
         "Media": [media(4, "720", 155, ["ger"], 4)]},
        {"type": "movie", "ratingKey": "12", "title": "Solo", "Guid": [{"id": "tmdb://1"}],
         "Media": [media(5, "1080", 100, ["ger"], 8)]},
    ]
    job = dupes.find(metas, dupes.load_prefs(None))
    assert {d.keep.media_id for d in job.items} == {2}  # German 1080p beats English 4K
    sure = sorted(d.drop.media_id for d in job.items if d.sure)
    unsure = [d.drop.media_id for d in job.items if not d.sure]
    assert sure == [1, 4] and unsure == [3]  # 140 of 156 minutes: maybe another cut, ask
    text, buttons = dupes.preview(job, dupes.load_prefs(None))
    assert "2 doppelte Versionen" in text and "❓ 1 ähnlich" in text and buttons[0][1] == "all"

    prefs = dupes.load_prefs('{"language_first": false}')
    job = dupes.find(metas, prefs)
    assert {d.keep.media_id for d in job.items} == {1}  # resolution first: 4K stays
    assert dupes.parse_resolutions("1080p vor 4K")[:3] == ["1080", "2160", "720"]


def test_duplicate_settings_are_saved(tmp_path):
    tools, _, _ = runner(tmp_path)
    res = asyncio.run(tools.run(ToolCall("1", "admin_duplicates",
                                         {"action": "settings", "similarity": 95, "resolutions": "1080p vor 4K",
                                          "languages": ["Deutsch", "Englisch"]}), ADMIN))
    assert "Deutsch vor Englisch" in res.final and "1080p vor 4K" in res.final and "95" in res.final
    assert tools._dupe_prefs()["similarity"] == 95


def test_duplicate_full_list_pages():
    metas = [{"type": "movie", "ratingKey": str(i), "title": f"Film {i}", "Guid": [{"id": f"tmdb://{i}"}],
              "Media": [media(i * 10, "1080", 100, ["ger"], 8), media(i * 10 + 1, "720", 100, ["ger"], 3)]}
             for i in range(1, 11)]
    job = dupes.find(metas, dupes.load_prefs(None))
    text, buttons = dupes.preview(job, dupes.load_prefs(None))
    assert "list" in [v for _, v in buttons]
    text, buttons = dupes.full_list(job)
    assert "Duplikate 1–8 von 10 (Seite 1/2)" in text and "🗑 720p" in text and "11.mkv" in text
    assert [v for _, v in buttons][0] == "pg:next"
    job.page = 1
    text, buttons = dupes.full_list(job)
    assert "9–10 von 10" in text and "pg:prev" in [v for _, v in buttons] and "pg:next" not in [v for _, v in buttons]
