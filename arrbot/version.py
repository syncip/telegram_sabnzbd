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

__version__ = "1.7.0"

CHANGES = {
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


async def latest(repo: str, branch: str = "main", transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """Version on GitHub, None if unknown (no network, repo private, ...)."""
    if not repo:
        return None
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/arrbot/version.py"
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as http:
            r = await http.get(url)
        if r.status_code != 200:
            log.info("Update-Check: %s antwortet %s", url, r.status_code)
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
