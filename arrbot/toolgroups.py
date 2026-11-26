"""Token saving: send the AI only the tools a message needs.

Every tool description costs input tokens on every AI call. Most messages are "lad X" or "wie weit ist
X", so the core tools are always there and the other groups only when the message (or the running
conversation) is about them. Picking happens here by keywords, without an AI call. If the AI still
misses something it calls more_tools and gets the full list for the rest of the answer.
"""

from __future__ import annotations

import re

from .llm import ToolSpec

CORE = {"search_movies", "search_series", "add_movie", "add_series", "offer_titles", "search_existing",
        "download_status", "open_url", "list_quality_profiles", "more_tools"}

GROUPS: dict[str, tuple[set[str], str]] = {
    "discover": ({"suggest", "top_titles", "remember", "my_profile", "wishlist", "suggest_together",
                  "title_feedback"},
                 r"vorschl|lustig|witzig|spannend|gruselig|romantisch|stimmung|laune|unter \d+ min|"
                 r"fand ich|war (super|toll|gut|mies|schlecht|doof|langweilig)|kenn(e)? ich|geschmack|"
                 r"studio|marvel|pixar|empfiehl|empfehl|was soll ich|schau(en)?\b|tipp|top|chart|beste|liste|trend|beliebt|"
                 r"ähnlich|wie .* (film|serie)|mag\b|mögen|hasse|merk|vergiss|profil|über mich|weißt du|vorlieb|"
                 r"genre|horror|komödie|action|thriller|doku|anime|neu(e|es|en)? (film|serie)|"
                 r"wunschliste|vormerk|erschein|rauskomm|termin|zusammen|gemeinsam|mit (mir|uns|anna|\w+) schau"),
    "german": ({"german_info", "german_fix", "notify_german", "clean_episodes"},
               r"deutsch|synchro|german|sprache|tonspur|englisch|original|kino|stream|netflix|prime|disney|"
               r"sky|wow|joyn|rtl|bescheid|meld|benachrichtig"),
    "manage": ({"remove_title", "pause_title", "resume_title", "clean_episodes"},
               r"lösch|entfern|\bweg\b|raus|pausier|später|nochmal suchen|in \d+ tag|weiter\b|fortsetz|stopp|"
               r"abbrech|nicht mehr"),
    "extras": ({"calendar", "disk_space", "weekly_digest", "plex_status", "bot_info", "mute_notifications",
                "notification_settings"},
               r"stumm|mute|still|ruhe|keine (nachricht|meldung|benachrichtigung|info)|meldung|nerv|laut\b|"
               r"benachrichtig|bescheid|info (wenn|bei|nur)|nur (noch )?(wenn|info)|"
               r"version|modell|\bki\b|update|bist du aktuell|kalender|woche|kommt|wann|heute|morgen|speicher|platz|festplatte|voll|rückblick|plex|gesehen|"
               r"geschaut|weiterschau|fortschritt|verknüpf|verbind"),
    "sites": ({"watch_site"}, r"überwach|beobacht|webseite|website|im auge|seiten?\b"),
    "admin": (set(),  # all admin_* tools, only for admins
              r"statistik|übersicht|dashboard|verwaltung|wie steht|was macht|zeig mir|nutzer|user|limit|check|api|mediathek|aufräum|qualität|profil|aktivit|anfrag|"
              r"sammlung|collection|problem|fehler|freigab|kosten|tokens|sperr|erlaub|admin|gesehen|ungesehen|platz|speicher|lösch|regel|"
              r"untertitel|überwach|webseite|website|behalt|schütz|nie löschen|frisst|größt|groß|duplikat|doppel|"
              r"version(en)?\b|auflösung|\bgb\b|tage (alt|da)|älter als"),
}

MORE = ToolSpec("more_tools", "Nur aufrufen, wenn für die Anfrage ein Werkzeug fehlt: dann gibt es alle.",
                {"type": "object", "properties": {}})


def groups_for(text: str) -> set[str]:
    low = (text or "").lower()
    return {g for g, (_, pattern) in GROUPS.items() if re.search(pattern, low)}


def select(specs: list[ToolSpec], groups: set[str]) -> list[ToolSpec]:
    """Core tools plus the chosen groups, in the original order (stable order = better caching)."""
    names = set(CORE)
    for g in groups:
        names |= GROUPS[g][0]
    known = {n for tools, _ in GROUPS.values() for n in tools}
    out = [s for s in specs
           if s.name in names
           or (s.name.startswith("admin_") and "admin" in groups)
           or (s.name not in known and not s.name.startswith("admin_"))]  # new tools are never hidden
    if len(out) < len(specs):
        out.append(MORE)
    return out
