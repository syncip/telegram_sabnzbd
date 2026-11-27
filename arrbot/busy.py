"""⏳ "Arbeitest du noch?" – answered at once, without the AI, even while a long answer is still running.

A chat's messages are answered one after the other (the conversation is locked), so a status question
would only be answered after the long job. Here it is caught before that: the bot says what it is doing,
since when, and which step is running right now.
"""

from __future__ import annotations

import re

STATUS = re.compile(
    r"^\s*(hallo\s*,?\s*)?("
    r"arbeitest du( da)? noch|bist du( noch)? (dran|da|beschäftigt|fertig|wach)|noch dran|"
    r"läuft (das|es|der (scan|vorgang)|die suche)( (eigentlich|denn))? noch|hängst du|hängt (das|es)|"
    r"(was )?machst du (gerade|grad|noch)|dauert (das|es)( noch)?( lange)?|wie lange (noch|dauert (das|es))|"
    r"status|lebst du( noch)?|bist du (abgestürzt|eingeschlafen)|alles (ok|okay|gut)( bei dir)?|"
    r"wie weit bist du|und\s*\?+|\?+|hallo\s*\?+"
    r")(\s*(noch|denn|eigentlich|gerade|grad))*\s*[?!.]*\s*$",
    re.I,
)

TOOL_DE = {
    "admin_duplicates": "Ich prüfe Plex auf Duplikate",
    "admin_delete_media": "Ich stelle die Löschliste zusammen (Radarr, Sonarr, Plex)",
    "admin_storage": "Ich rechne den Speicher aus",
    "admin_watch_questions": "Ich schaue in deinen Plex-Verlauf",
    "suggest": "Ich suche Vorschläge",
    "suggest_together": "Ich suche gemeinsame Vorschläge",
    "top_titles": "Ich hole die Toplisten",
    "search_movies": "Ich suche den Film",
    "search_series": "Ich suche die Serie",
    "add_movie": "Ich füge den Film hinzu",
    "add_series": "Ich füge die Serie hinzu",
    "search_existing": "Ich suche nochmal bei den Indexern",
    "german_info": "Ich prüfe, ob es das auf Deutsch gibt (Indexer, TMDB)",
    "german_fix": "Ich suche deutsche Releases",
    "clean_episodes": "Ich prüfe die Folgen",
    "download_status": "Ich schaue nach den Downloads",
    "admin_check": "Ich prüfe die Dienste",
    "build_tool": "Ich baue ein neues Werkzeug",
}


def is_status_question(text: str, busy: bool = True) -> bool:
    """While busy also "?" or "und?"; otherwise only real questions ("arbeitest du noch?")."""
    if not STATUS.match(text or ""):
        return False
    return busy or bool(re.search(r"[a-zäöü]{4,}", (text or "").lower().replace("hallo", "")))


def _ago(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    return f"{m} Min." if m >= 5 or not s else f"{m} Min. {s} s"


def status_text(question: str | None, started: float | None, tool: str | None, tool_started: float | None,
                now: float) -> str:
    if started is None:
        return "👍 Ich bin frei – gerade läuft bei mir nichts. Downloads siehst du mit /downloads."
    q = (question or "").strip().replace("\n", " ")
    q = q[:60] + ("…" if len(q) > 60 else "")
    lines = [f"⏳ Ja, ich arbeite noch an „{q}“ (seit {_ago(now - started)})."]
    if tool:
        step = TOOL_DE.get(tool, f"Schritt: {tool}")
        lines.append(f"Gerade: {step}" + (f" – seit {_ago(now - tool_started)}." if tool_started else "."))
    else:
        lines.append("Gerade: Ich warte auf die KI.")
    lines.append("Ich melde mich, sobald ich fertig bin.")
    return "\n".join(lines)
