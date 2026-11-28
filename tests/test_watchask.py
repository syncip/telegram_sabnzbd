"""Watched in Plex -> "löschen?" now and then; series seasons from Sonarr; other languages only."""

import asyncio
from datetime import datetime, timedelta, timezone

from arrbot import langpick, watchask
from arrbot.cleanup import RuleStore
from arrbot.media import Report, Title
from arrbot.tools import Candidate, Flow, ToolRunner
from arrbot.state import Store

ADMIN = 1
NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)


def movie(arr_id, title, days_ago, size=5 * 2**30):
    t = Title("movie", arr_id, title, 2020, size, None, True, 1, 1, ext_id=arr_id)
    t.complete_by, t.started_by = {ADMIN}, {ADMIN}
    t.viewed_by = {ADMIN: NOW - timedelta(days=days_ago)}
    return t


def test_settings_and_due(tmp_path):
    store = RuleStore(str(tmp_path))
    s = watchask.load(store)
    assert s.on and s.every == 3 and "alle 3 Tage höchstens 3" in s.text()
    assert watchask.due(store, s, NOW)
    watchask.mark_round(store, NOW)
    assert not watchask.due(store, s, NOW + timedelta(days=1))
    assert watchask.due(store, s, NOW + timedelta(days=3))
    s.on = False
    watchask.save(store, s)
    assert not watchask.load(store).on and "aus" in watchask.load(store).text()


def test_candidates_and_answers(tmp_path):
    store = RuleStore(str(tmp_path))
    report = Report([movie(1, "Dune", 5), movie(2, "Heute", 0), movie(3, "Behalten", 9)], [ADMIN], True, [])
    movies, series = watchask.candidates(report, ADMIN, {("radarr", 3)}, {}, 2, NOW)
    assert [t.title for t in movies] == ["Dune"] and series == []  # too fresh / kept are out
    a = watchask.Ask(movies[0], [])
    assert a.key == "radarr:1" and not watchask._answered(store, a.key, NOW)
    watchask.answer(store, a.key, "later", NOW)
    assert watchask._answered(store, a.key, NOW + timedelta(days=13))
    assert not watchask._answered(store, a.key, NOW + timedelta(days=15))
    watchask.answer(store, a.key, "no", NOW)
    assert watchask._answered(store, a.key, NOW + timedelta(days=400))


def test_series_seen_seasons():
    t = Title("series", 7, "Dark", 2017, 30 * 2**30, None, True, 5, 5, ended=True, ext_id=70)
    t.started_by, t.viewed_by = {ADMIN}, {ADMIN: NOW - timedelta(days=4)}
    eps = [{"id": i, "seasonNumber": s, "episodeNumber": e, "hasFile": True, "episodeFileId": 100 + i,
            "episodeFile": {"size": 2**30}} for i, (s, e) in enumerate([(1, 1), (1, 2), (2, 1), (2, 2), (3, 1)])]
    leaves = [{"parentIndex": s, "index": e, "viewCount": 1, "lastViewedAt": int((NOW - timedelta(days=4)).timestamp())}
              for s, e in [(1, 1), (1, 2), (2, 1), (2, 2)]]
    a = watchask.series_ask(t, eps, leaves, ADMIN, NOW)
    assert a.key == "sonarr:7:1,2" and a.whole is None  # season 3 not seen yet
    text, buttons = watchask.question(a)
    assert "Staffel 1, Staffel 2" in text and [v for _, v in buttons] == ["del", "no", "later", "keep"]
    leaves.append({"parentIndex": 3, "index": 1, "viewCount": 1, "lastViewedAt": leaves[0]["lastViewedAt"]})
    a = watchask.series_ask(t, eps, leaves, ADMIN, NOW)
    assert a.whole is not None and "whole" in [v for _, v in watchask.question(a)[1]]


class FakeRadarr:
    def __init__(self):
        self.deleted = []

    async def delete(self, arr_id, delete_files):
        self.deleted.append(arr_id)


def test_flow_delete_and_keep(tmp_path):
    tools = ToolRunner(FakeRadarr(), None, Store(str(tmp_path)), "released")
    tools.cleanup = RuleStore(str(tmp_path))
    t = movie(1, "Dune", 5)
    from arrbot import mediamgr

    def ask_flow():
        a = watchask.Ask(t, [mediamgr.whole(t)])
        f = Flow("f1", ADMIN, Candidate("movie", 0, "Dune", None), stage="wa", data=a)
        tools.flows[f.id] = f
        return f

    ask_flow()
    flow = asyncio.run(tools.choose("f1", "del"))
    assert flow.step.done and "Gelöscht" in flow.step.text and tools.radarr.deleted == [1]
    assert watchask._answered(tools.cleanup, "radarr:1", datetime.now(timezone.utc))
    ask_flow()
    asyncio.run(tools.choose("f1", "keep"))
    assert ("radarr", 1) in tools.cleanup.kept()


def rel(title, langs, rejected=True, why="Language is not wanted in profile", res=1080, **kw):
    return {"title": title, "guid": title, "indexerId": 1, "rejected": rejected, "rejections": [why] if rejected else [],
            "languages": [{"name": x} for x in langs], "quality": {"quality": {"resolution": res}}, **kw}


def test_lang_offer_and_pick():
    rels = [rel("Show.S01.KOREAN.1080p", ["Korean"], fullSeason=True),
            rel("Show.S01.2160p", ["English"], res=2160, fullSeason=True),
            rel("Show.S01.720p", ["English"], res=720, fullSeason=True),
            rel("Show.S01.MULTi.1080p", ["English", "Korean"], fullSeason=True)]
    o = langpick.build("series", 5, 1, ADMIN, "Show", "HD-1080p Deutsch", rels)
    assert set(o.groups) == {"Koreanisch", "Englisch", "Multi"}
    assert "nur auf" in o.text() and "„HD-1080p Deutsch“ passt dafür im Zweifel nicht" in o.text()
    assert [r["title"] for r in langpick.pick(o.groups["Englisch"], 1080)] == ["Show.S01.720p"]  # not above 1080p
    assert langpick.build("series", 5, 1, ADMIN, "Show", "", rels + [rel("Show.S01.GERMAN", ["German"])]) is None
    assert langpick.build("series", 5, 1, ADMIN, "Show", "", [rel("ok", ["English"], rejected=False)]) is None
    eps = [rel("Show.S01E01", ["English"], episodeNumbers=[1]), rel("Show.S01E01E02", ["English"], episodeNumbers=[1, 2]),
           rel("Show.S01E03", ["English"], episodeNumbers=[3])]
    assert [r["title"] for r in langpick.pick(eps)] == ["Show.S01E01E02", "Show.S01E03"]


class FakeSonarr:
    async def lookup_tvdb(self, tvdb_id):
        return {"tvdbId": tvdb_id, "title": "Dark", "seasons": [{"seasonNumber": n} for n in (0, 1, 2, 3)]}


def test_suggested_series_asks_for_seasons(tmp_path):
    """Suggestions come from TMDB without seasons: the bot asks Sonarr and then asks which seasons."""
    tools = ToolRunner(None, FakeSonarr(), Store(str(tmp_path)), "released")
    flow = asyncio.run(tools._start_flow(ADMIN, Candidate("series", 70, "Dark", 2017)))
    assert flow.stage == "seasons" and "Welche Staffeln" in flow.step.text
    assert [v for _, v in flow.step.buttons][:4] == ["all", "s1", "s2", "s3"]


def test_choices_per_quality_and_language():
    def r(title, langs, res, rejected=True, reason="Quality not wanted in profile"):
        return {"title": title, "guid": title, "indexerId": 1, "fullSeason": True, "rejected": rejected,
                "rejections": [reason] if rejected else [], "languages": [{"name": x} for x in langs],
                "quality": {"quality": {"name": f"WEBDL-{res}p", "resolution": res}}}
    rels = [r("Show.S01.German.720p", ["German"], 720), r("Show.S01.German.1080p", ["German"], 1080, False),
            r("Show.S01.1080p", ["English"], 1080), r("Show.S01.MULTi.1080p", ["German", "English"], 1080),
            r("Show.S01.German.480p", ["German"], 480, reason="Unable to identify correct episode(s)")]
    o = langpick.choices("series", 5, 1, ADMIN, "Show", "Deutsch 1080p", rels)
    assert list(o.groups) == ["1080p Deutsch", "720p Deutsch", "1080p Englisch", "1080p Multi"]
    assert o.button("1080p Multi") == "⬇️ 1080p Multi (DE/EN) laden"
    assert o.button("720p Deutsch") == "⬇️ 720p Deutsch laden" and o.done("720p Deutsch") == "in 720p Deutsch"
    assert [x["title"] for x in langpick.pick(o.groups["720p Deutsch"], o.targets["720p Deutsch"])] == \
        ["Show.S01.German.720p"]
    assert langpick.choices("series", 5, 1, ADMIN, "Show", "", []) is None
