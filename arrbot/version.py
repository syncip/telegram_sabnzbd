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

__version__ = "1.24.2"

CHANGES = {
    "1.24.2": "📚 Bücher kommen sofort: Während ein angefordertes Buch lädt, schaut der Bot alle 5 Sekunden in "
              "SABnzbd. Ist der Download fertig, stößt er den Import in Chaptarr an und schickt das Buch, sobald "
              "die Datei da ist – ohne Rundenwarten. Lädt nichts, fragt er auch nichts ab",
    "1.24.1": "📚 Format: Die KI darf es nicht mehr selbst festlegen – der Bot fragt, solange du keins gewählt oder "
              "genannt hast. Schickt er im gemerkten Format, kommt darunter „Lieber ein anderes Format?“ mit Buttons. "
              "/buecher: „🔄 Neu prüfen“ (Chaptarr scannt neu, gelöschte Bücher fliegen aus deiner Liste) und "
              "„🧹 Liste leeren“",
    "1.24.0": "📚 Bessere Buch-Empfehlungen: jeder Vorschlag wird in Chaptarr geprüft (Erfundenes fliegt raus, echtes "
              "Jahr, 📗 = schon da), deine Bücher werden nicht nochmal vorgeschlagen und prägen den Geschmack, "
              "„aktuelle Krimis“ nutzt Neuerscheinungen und Trends von Open Library und zeigt nur, was in den "
              "letzten ~18 Monaten erschienen ist. Fertige Bücher kommen schneller: Prüfung alle 30 s, der Bot "
              "stößt den Import in Chaptarr an und sagt Bescheid, wenn Chaptarr ein Buch nicht übernehmen kann",
    "1.23.0": "📚 /buecher: deine Bücher übersichtlich mit Seiten (8 pro Seite), 📗 da / 🔎 wird gesucht / ⏳ wird "
              "vorbereitet, 📥-Nummern-Buttons zum erneuten Schicken – die Liste bleibt danach offen; Umschalten auf "
              "alle Bücher auf dem Server. /check zeigt, ob der Bot die Bücher-Dateien lesen kann (mit Tipp für "
              "BOOK_PATHS) und ob Calibre zum Umwandeln da ist",
    "1.22.2": "Buchauswahl lesbar: mehrere Ausgaben desselben Buchs werden zu einem zusammengefasst, die vollen "
              "Titel (mit Jahr und Seiten) stehen nummeriert in der Nachricht, die Buttons sind kurz. Bereitet "
              "Chaptarr ein Buch noch vor („being prepared by the metadata server“), kommt kein Fehler mehr: der "
              "Bot wartet, lässt suchen, sobald es drin ist, und schickt es",
    "1.22.1": "Der Bot weiß, was er außerhalb des Gesprächs geschickt hat (Ergebnis nach einem Button, „📚 … ist "
              "da“): „Schick mir das letzte Buch nochmal“ klappt jetzt. „Welche Bücher haben wir?“ zeigt die "
              "Bücher aus Chaptarr mit Buttons zum Schicken",
    "1.22.0": "📚 Bücher im Wunschformat: Der Bot fragt einmal EPUB, PDF oder Kindle, merkt es sich und wandelt "
              "um, wenn es das Buch so nicht gibt (Calibre im Image). Buch-Empfehlungen: „Bücher wie Metro“ – fünf "
              "Vorschläge mit Grund und Buttons zum Holen. Weniger Warnungen: Sonarr/Radarr/Chaptarr-Meldungen erst, "
              "wenn sie zwei Runden bleiben, alle in einer Nachricht, ohne Link-Vorschau, mit „🔕 nicht mehr melden“; "
              "abgeschaltete Optionen (RSS aus) werden erklärt statt sinnlos neu getestet",
    "1.21.0": "Chaptarr wie Radarr/Sonarr eingebunden: /check prüft es (Version, Profile, Warnungen), die "
              "Überwachung meldet Ausfall und Rückkehr, Chaptarr-Warnungen kommen wie bei Sonarr/Radarr (Indexer/"
              "Download-Client wird selbst neu getestet), laufende Buch-Downloads stehen in /downloads und der "
              "Bücher-Ordner im Speicherplatz",
    "1.20.0": "📚 Bücher mit Chaptarr: „Schick mir das Buch X“ – ist das E-Book da, kommt die Datei direkt in den "
              "Chat; sonst sucht Chaptarr es und der Bot schickt es, sobald es geladen ist",
    "1.19.0": "Vorschläge denken mit: „was für nebenbei beim Cardio“, „zum Einschlafen“, „mit den Kindern“ – "
              "passende Genres und kurze Folgen statt nur des eigenen Geschmacks, mit Folgenlänge; auch "
              "„Schon auf dem Server“-Tipps müssen passen",
    "1.18.1": "Intern aufgeräumt, gleiche Funktionen: die Werkzeuge sind nach Thema auf mehrere Dateien verteilt "
              "(Mediathek, Admin, Vorschläge, Deutsch), dazu eine Übersicht, wo was liegt",
    "1.18.0": "Duplikate anzeigen: „Zeig mir die Duplikate“ bzw. „📋 Alle anzeigen“ – die ganze Liste seitenweise "
              "mit Dateinamen, was weg soll und was bleibt; große Bibliotheken werden nicht mehr abgeschnitten. "
              "„Arbeitest du noch?“ beantwortet der Bot sofort, auch mitten in langen Vorgängen: woran, seit "
              "wann, welcher Schritt",
    "1.17.0": "Serien aus Vorschlägen und Toplisten fragen wieder nach den Staffeln (alle, Staffel X, nur neue). "
              "Nachfragen zu Gesehenem: Film oder Staffeln in Plex fertig geschaut, dann fragt der Bot hin und "
              "wieder, ob es weg kann (Löschen, Behalten, Später, Nie löschen). Nur in anderer Sprache: Der Bot "
              "sagt, welche Sprachen es gibt und dass dein Profil im Zweifel nicht passt, und lädt per Button "
              "auf Koreanisch, Englisch oder Multi",
    "1.16.0": "Gesamtlimits pro Tag: Tokens und Kosten für den ganzen Bot (oder pro Nutzer), Warnung an den "
              "Admin bei 80 % und 100 %. Limit-Empfehlung („Welches Limit sollte ich setzen?“, /limit empfehlung): "
              "aus dem Verbrauch der letzten 30 Tage und der Zahl aktiver Nutzer, mit Sicherheitsaufschlag, "
              "per Button übernehmen",
    "1.15.0": "Sicherheit: Nicht freigegebene Nutzer bekommen genau eine Antwort („Admin ist gefragt“), dann "
              "nichts mehr – nichts erreicht die KI, keine Werkzeuge, keine Buttons; Bescheid bei Freigabe. "
              "Buttons gelten nur im eigenen Chat. Titel-Limits: Filme/Serien/Staffeln/Folgen pro Stunde, Tag, "
              "Woche, Monat (auch für „such nochmal“). Rechte pro Nutzer: „Sarah darf ihre Filme und Serien "
              "löschen“, pausieren, Reparatur, Medien-Manager, keine Limits",
    "1.14.0": "Medien-Manager für den Admin: aufräumen per Satz („Lösche alle Filme, die länger als 180 Tage da "
              "sind und die ich nicht gesehen habe“) mit Liste und Bestätigung, bei Serien Folgen/Staffeln/ganze "
              "Serie per Button, direkt über Radarr/Sonarr ohne Sperre; geschützt bleiben „Nie löschen“, was "
              "andere gerade schauen oder neu angefragt haben. Speicher-Übersicht („Was frisst Platz?“). "
              "Duplikat-Finder in Plex: gleicher Inhalt (Laufzeit, Ähnlichkeit einstellbar), behält die "
              "bevorzugte Sprache und Auflösung",
    "1.13.0": "Bessere Vorschläge: Stimmung und Länge („was Lustiges unter 90 Minuten“), „wie X, aber …“, "
              "nur auf Deutsch, Abwechslung (ein Teil pro Filmreihe, 💎 Geheimtipp), Begründung pro Titel, "
              "„📼 Schon auf dem Server“; 👎/👁 pro Titel, „Wie fandest du’s?“ mit Sternen (auch nach Plex), "
              "Plex-Sterne zählen, Kennenlernen für neue Nutzer. Meldungen: /meldungen (z. B. nur „fertig“), "
              "/stumm auch für Stunden und Tage; der Bot fragt nach, wenn etwas unklar ist",
    "1.12.0": "Sonarr/Radarr-Warnungen: neue werden gemeldet (mit Erklärung), behobene auch; gesperrte "
              "Indexer/Download-Clients testet der Bot selbst neu; Indexer nur mit Hauptkategorien: Button "
              "„Unterkategorien eintragen“; /check zeigt die Warnungen. MIT-Lizenz",
    "1.11.0": "Chat-Log: /log und /debug an zeigen jeden Schritt (KI, Werkzeuge, Sonarr/Radarr-Anfragen, "
              "Suchergebnis). Suche ohne Ergebnis: der Bot sagt sofort, warum (nicht überwacht, Indexer-Kategorien, "
              "abgelehnte Releases mit Qualität und Grund). Staffel einer vorhandenen Serie laden schaltet die "
              "Überwachung ein. Viele Folgen: eine Meldung beim Start, eine Zusammenfassung am Ende. "
              "/stumm und /laut (auch per Text)",
    "1.10.0": "OpenAI über die Responses-API: gpt-6-luna denkt wieder nach (Stufe low) und nutzt dabei Werkzeuge; nichts wird bei OpenAI gespeichert. Andere Server bleiben bei Chat Completions (LLM_API). „Such nochmal“ für Titel in der Bibliothek: auch per deutschem Titel, nur fehlende Folgen, Staffelpaket bei ganzen Staffeln, schaltet die Überwachung ein",
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
