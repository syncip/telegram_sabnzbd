"""Cleanup rules (offline)."""

from datetime import datetime, timedelta, timezone

from arrbot import cleanup
from arrbot.media import Report, Title

NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)


def title(i, kind, seen_days=None, added_days=400, started=False, size=10):
    t = Title(kind, i, f"T{i}", 2020, size * 2**30, NOW - timedelta(days=added_days), True, 1, 1, True, 100 + i)
    t.in_plex = True
    if seen_days is not None:
        t.complete_by, t.last_viewed = [1], NOW - timedelta(days=seen_days)
    if started or seen_days is not None:
        t.started_by = [1]
    return t


def test_rules_match_watched_and_unwatched(tmp_path):
    store = cleanup.RuleStore(str(tmp_path))
    r1 = store.add("watched", "movie", 30)
    r2 = store.add("unwatched", "all", 365, low_space_gb=100, auto=True)
    assert [r.id for r in store.rules()] == [r1.id, r2.id] and store.rules()[1].auto
    assert "nur wenn unter 100 GB frei" in r2.text and "löschen automatisch" in r2.text

    rep = Report([title(1, "movie", seen_days=40), title(2, "movie", seen_days=5), title(3, "series", seen_days=90),
                  title(4, "movie"), title(5, "series", added_days=30), title(6, "movie", started=True)],
                 [1], True, made=NOW)
    assert [t.title for t in cleanup.matches(r1, rep, set(), NOW)] == ["T1"]  # T2 seen too recently, T3 a series
    assert [t.title for t in cleanup.matches(r2, rep, set(), NOW)] == ["T4"]  # T5 too new, T6 started
    store.keep("radarr", 1, "T1")
    assert cleanup.matches(r1, rep, store.kept(), NOW) == []

    text, buttons = cleanup.render_hits(r2, [rep.titles[3]])
    assert "1 Titel passen · zusammen 10,0 GB" in text and buttons[0] == ("🗑 Alle 1 löschen (10,0 GB)", f"c:all:{r2.id}")
    assert buttons[1][1] == "c:keep:radarr:4"
    assert store.remove(r1.id) and not store.remove(999)
    assert "Aufräumregeln" in cleanup.render_rules(store.rules())
