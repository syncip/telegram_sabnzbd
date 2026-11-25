"""Version of the bot and the update check against GitHub.

Raise __version__ (and add a line to CHANGES) with every release. The bot compares it with the
version on GitHub (arrbot/version.py on the main branch of UPDATE_REPO) once a day.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

__version__ = "1.9.1"

CHANGES = {
    "1.9.1": "Fix: KI-Fehler 400 mit gpt-6-luna (Werkzeuge nur ohne Denk-Stufe) – Standard jetzt „none“, bei Ablehnung schaltet der Bot selbst um",
    "1.9.0": "/admin-Übersicht mit Buttons, Nutzerkarte (ℹ️ in /nutzer, „Zeig mir Anna“), Version und KI-Modell "
             "per /version oder Frage („Welches Modell nutzt du?“), Modelle mit Preis und Abschalt-Warnung in "
             "/check; Deutsch-Prüfung: „Melden, sobald es Deutsch gibt“ und „Sonarr neu einlesen“ statt Sackgasse; "
             "fertiges Docker-Image auf ghcr.io",
    "1.8.1": "Neue Standard-Modelle: gpt-6-luna für den Bot (günstiger als gpt-4o-mini, das OpenAI im Januar "
             "abschaltet), gpt-6.1-terra für selbstgebaute Werkzeuge; Denk-Stufe einstellbar (LLM_REASONING), "
             "/check prüft auch, ob die KI Werkzeuge aufruft",
    "1.8.0": "Nutzer dürfen nur noch suchen und hinzufügen (Löschen, Pausieren, Austauschen nur Admin), "
             "Höchstqualität pro Nutzer oder für alle, Spam-Schutz mit vorsorglicher Sperre und Warnungen, "
             "Monatsbericht (/monatsbericht), selbstgebaute Werkzeuge für den Admin (/rezepte), "
             "Wunschliste für Unveröffentlichtes, „Was können wir zusammen schauen?“, Plex-Sammlungen, "
             "Top-Listen pro Studio, Deutsch-Prüfung über alle Staffeln",
    "1.7.0": "Aktivität (/aktivitaet), Überwachung der Dienste, Versionsnummer mit Update-Hinweis, "
             "🎧 Reinhören (echte Sprache statt Tonspur-Markierung), weniger Tokens (nur passende Werkzeuge), "
             "„Neu in Plex“, Aufräumregeln (/regeln), Freigaben (/freigaben), Buttons überleben Neustarts, "
             "Webseiten beobachten",
    "1.6.0": "Plex, Kalender, Speicherplatz, Deutsch-Benachrichtigung, Wochenrückblick, Mediathek, "
             "echte Tonspur-Prüfung, Qualitätsprofile in normaler Sprache",
    "1.5.0": "Nutzerprofile: Vorlieben lernen, bessere Vorschläge",
    "1.4.0": "Admin in normaler Sprache, Statistik, API-Check, deutsche Folgen",
}


def parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def newer(a: str, b: str) -> bool:
    """Is a newer than b?"""
    return parse(a) > parse(b)


async def latest(repo: str, branch: str = "main", transport: httpx.AsyncBaseTransport | None = None,
                 token: str | None = None) -> str | None:
    """Version on GitHub, None if unknown (no network, private repo without token, ...).
    token: read-only GitHub token, needed for a private repository."""
    if not repo:
        return None
    if token:
        url = f"https://api.github.com/repos/{repo}/contents/arrbot/version.py?ref={branch}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json"}
    else:
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/arrbot/version.py"
        headers = {}
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as http:
            r = await http.get(url, headers=headers)
        if r.status_code != 200:
            log.info("Update-Check: GitHub antwortet %s%s", r.status_code,
                     " (privates Repo? UPDATE_TOKEN setzen)" if r.status_code == 404 and not token else "")
            return None
        m = re.search(r'__version__\s*=\s*"([^"]+)"', r.text)
        return m.group(1) if m else None
    except httpx.HTTPError as e:
        log.info("Update-Check nicht möglich: %s", e)
        return None


def line(latest_version: str | None) -> str:
    """'🏷 Version 1.7.0 – aktuell ✅' for /check, /hilfe and /version."""
    if latest_version is None:
        return f"🏷 Version {__version__}"
    if newer(latest_version, __version__):
        return f"🏷 Version {__version__} – ⬆️ neu verfügbar: {latest_version}"
    return f"🏷 Version {__version__} – aktuell ✅"


def text(latest_version: str | None) -> str:
    out = [line(latest_version)]
    if latest_version and newer(latest_version, __version__):
        out.append("Update: Container neu bauen bzw. neues Image ziehen und neu starten.")
    if __version__ in CHANGES:
        out.append(f"\nNeu in {__version__}: {CHANGES[__version__]}")
    return "\n".join(out)


def first_start_of_version(data_dir: str) -> str | None:
    """The previous version if this one runs for the first time (then the admin hears about it)."""
    path = Path(data_dir) / "version.txt"
    try:
        old = path.read_text(encoding="utf-8").strip()
    except OSError:
        old = None
    if old == __version__:
        return None
    try:
        path.write_text(__version__, encoding="utf-8")
    except OSError as e:
        log.warning("version.txt nicht geschrieben: %s", e)
    return old or "neu"
