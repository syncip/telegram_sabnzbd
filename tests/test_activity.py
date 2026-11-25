"""Activity log for the admin (offline)."""

from zoneinfo import ZoneInfo

from arrbot import activity


def test_classify():
    assert activity.classify([], [], "Hallo!", None) == (activity.TALK, "")
    assert activity.classify(["search_movies"], [], "Hier", None) == (activity.OK, "")
    assert activity.classify(["search_movies"], [], "🤷 Nichts gefunden zu Xyz", None)[0] == activity.NOTHING
    assert activity.classify(["add_movie"], ["Radarr nicht erreichbar"], "", None) == (
        activity.PROBLEM, "Radarr nicht erreichbar")
    assert activity.classify([], [], "⚠️ KI-Fehler", None) == (activity.ERROR, "KI-Fehler")
    assert activity.classify([], [], "x", "TimeoutError") == (activity.ERROR, "TimeoutError")


def test_store_and_render(tmp_path):
    db = activity.ActivityStore(str(tmp_path))
    db.record(1, "Lad Dune", ["search_movies", "add_movie"], activity.OK, tokens=900)
    db.record(2, "Lad Xyzzy", ["search_movies"], activity.NOTHING, "🤷 nichts gefunden")
    db.record(2, "Downloads?", ["download_status"], activity.PROBLEM, "SABnzbd: Timeout")
    db.record(1, "➕ Dune", [], activity.ACTION)
    names = {1: "Anna", 2: "Ben"}.get
    es = db.entries(7)
    assert len(es) == 4 and es[0].text == "➕ Dune"
    text = activity.render(es, 7, ZoneInfo("Europe/Berlin"), names)
    assert "3 Anfragen" in text and "⚠️ Probleme (2)" in text and "SABnzbd: Timeout" in text
    assert "Filmsuche 2" in text and "Erfolgsquote: 33 %" in text
    text = activity.render(es, 7, ZoneInfo("Europe/Berlin"), names, "users")
    assert "<b>Anna</b>: 2 · ✅ 2" in text and "<b>Ben</b>: 2 · ✅ 0 · ⚠️ 2" in text
    assert len(db.entries(7, 2)) == 2
    db.forget(2)
    assert len(db.entries(7)) == 2
