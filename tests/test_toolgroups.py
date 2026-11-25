"""Token saving: only the tools a message needs (offline)."""

from arrbot import toolgroups
from arrbot.tools import build_admin_specs, build_specs


def names(text, admin=False):
    specs = build_specs(True, True, True) + (build_admin_specs() if admin else [])
    return {s.name for s in toolgroups.select(specs, toolgroups.groups_for(text))}


def test_simple_request_gets_core_only():
    n = names("Lad Dune 2")
    assert {"search_movies", "add_movie", "more_tools"} <= n
    assert not n & {"suggest", "german_fix", "calendar", "remove_title"}


def test_topics_bring_their_tools():
    assert "german_fix" in names("Ist Dinosaurs wirklich auf Deutsch?")
    assert "suggest" in names("Was soll ich heute schauen?")
    assert "remove_title" in names("Lösch Dune")
    assert "calendar" in names("Was kommt diese Woche?")
    assert "admin_activity" in names("Zeig mir die Probleme der Nutzer", admin=True)
    assert "admin_activity" not in names("Lad Dune", admin=True)


def test_everything_when_all_groups_match():
    specs = build_specs(True, True, True)
    assert toolgroups.select(specs, set(toolgroups.GROUPS)) == specs
