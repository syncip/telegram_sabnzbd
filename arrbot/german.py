"""'Gibt es X schon auf Deutsch?' – answered in code from real sources, never paraphrased by the AI.

Series, per episode of one season:
  1. your library (Sonarr): downloaded file and its audio languages
  2. your indexers (Sonarr release search): German releases per episode / season pack
  3. TMDB: a real German episode title hints at a German broadcast; original air dates
Movies: German cinema / digital / disc dates and streaming (TMDB), file languages (Radarr), indexers.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from .arr import ArrError
from .tmdb import TMDB, TmdbError

log = logging.getLogger(__name__)

MAX_EPISODES = 30


def _langs(obj: dict | None) -> list[str]:
    if not obj:
        return []
    names = [(lng.get("name") or "") for lng in obj.get("languages") or []]
    if isinstance(obj.get("language"), dict):  # Sonarr v3
        names.append(obj["language"].get("name") or "")
    return [n for n in names if n]


AUDIO_CODES = {
    "ger": "German", "deu": "German", "de": "German", "german": "German", "deutsch": "German",
    "eng": "English", "en": "English", "english": "English", "fre": "French", "fra": "French", "fr": "French",
    "french": "French", "spa": "Spanish", "es": "Spanish", "spanish": "Spanish", "ita": "Italian", "it": "Italian",
    "italian": "Italian", "jpn": "Japanese", "ja": "Japanese", "japanese": "Japanese", "kor": "Korean",
    "ko": "Korean", "korean": "Korean", "rus": "Russian", "ru": "Russian", "russian": "Russian",
    "pol": "Polish", "pl": "Polish", "polish": "Polish", "tur": "Turkish", "tr": "Turkish", "turkish": "Turkish",
    "dut": "Dutch", "nld": "Dutch", "nl": "Dutch", "dutch": "Dutch",
}
LANG_DE = {"German": "Deutsch", "English": "Englisch", "French": "Französisch", "Spanish": "Spanisch",
           "Italian": "Italienisch", "Japanese": "Japanisch", "Korean": "Koreanisch", "Russian": "Russisch",
           "Polish": "Polnisch", "Turkish": "Türkisch", "Dutch": "Niederländisch"}


def audio_langs(file: dict | None) -> list[str] | None:
    """The real audio tracks of a file (Sonarr/Radarr read them on import). None = not known."""
    raw = ((file or {}).get("mediaInfo") or {}).get("audioLanguages")
    if not raw or not isinstance(raw, str):
        return None
    names = []
    for part in re.split(r"[/,|]", raw):
        n = part.strip()
        if not n or n.lower() in ("und", "unknown", "zxx", "mul"):
            continue
        names.append(AUDIO_CODES.get(n.lower(), n.title()))
    return list(dict.fromkeys(names)) or None


def file_langs(file: dict | None) -> list[str]:
    """Languages of a file: the real audio tracks when known, otherwise what the release name said."""
    return audio_langs(file) or _langs(file)


def file_check(file: dict | None) -> str:
    """german | mislabelled (name says German, audio isn't) | other | unknown."""
    audio, named = audio_langs(file), _langs(file)
    if audio is not None:
        if "German" in audio:
            return "german"
        return "mislabelled" if "German" in named else "other"
    if "German" in named:
        return "german"
    return "other" if named else "unknown"


def langs_de(langs: list[str]) -> str:
    return ", ".join(LANG_DE.get(x, x) for x in langs) or "unbekannt"


def _is_german_release(r: dict) -> bool:
    if "german" in [n.lower() for n in _langs(r)]:
        return True
    title = "." + (r.get("title") or "").lower().replace(" ", ".") + "."
    return ".german." in title


def _d(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "?"


def _air(ep: dict) -> date | None:
    raw = ep.get("airDateUtc") or ep.get("airDate")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except ValueError:
        return None


async def series_report(sonarr: Any, tmdb: TMDB | None, tvdb_id: int, season: int | None,
                        indexer: bool, today: date) -> str:
    series = await sonarr.lookup_tvdb(tvdb_id)
    lib_id = series.get("id")
    tmdb_id = None
    if tmdb:
        try:
            tmdb_id = series.get("tmdbId") or await tmdb.tv_from_tvdb(tvdb_id)
        except TmdbError as e:
            log.info("TMDB: %s", e)

    # ---- episodes: library first, TMDB otherwise -------------------------------------------
    lib_eps: dict[int, dict] = {}
    all_lib: list[dict] = []
    if lib_id:
        all_lib = [e for e in await sonarr.episodes(lib_id) if e.get("seasonNumber", 0) > 0]
    if season is None:
        aired = [e["seasonNumber"] for e in all_lib if (_air(e) or date.max) <= today]
        season = max(aired) if aired else max(
            (s["seasonNumber"] for s in series.get("seasons") or [] if s.get("seasonNumber", 0) > 0), default=1)
    lib_eps = {e["episodeNumber"]: e for e in all_lib if e["seasonNumber"] == season}

    titles: dict[int, dict] = {}
    if tmdb and tmdb_id:
        try:
            titles = await tmdb.season_titles(tmdb_id, season)
            # Without a library the newest TMDB season may be only announced: step back once.
            if not lib_eps and titles and all((t["air"] or date.max) > today for t in titles.values()) and season > 1:
                season -= 1
                titles = await tmdb.season_titles(tmdb_id, season)
        except TmdbError as e:
            log.info("TMDB Staffel: %s", e)

    numbers = sorted(set(lib_eps) | set(titles))
    if not numbers:
        return f"🤷 Zu **{series.get('title')}** Staffel {season} habe ich keine Folgen gefunden."

    # ---- indexers ------------------------------------------------------------------------
    german_rel: dict[int, int] = {}
    any_rel: dict[int, int] = {}
    indexer_note = ""
    if lib_id and indexer:
        try:
            for r in await sonarr.releases(seriesId=lib_id, seasonNumber=season):
                eps = numbers if r.get("fullSeason") else (r.get("mappedEpisodeNumbers") or r.get("episodeNumbers") or [])
                for n in eps:
                    any_rel[n] = any_rel.get(n, 0) + 1
                    if _is_german_release(r):
                        german_rel[n] = german_rel.get(n, 0) + 1
        except ArrError as e:
            indexer_note = f"\n⚠️ Indexer-Suche fehlgeschlagen: {e}"
    elif not lib_id:
        indexer_note = "\nℹ️ Nicht in deiner Bibliothek – ob es deutsche Downloads gibt, kann ich erst nach dem Hinzufügen prüfen."

    # ---- one line per episode ------------------------------------------------------------------
    lines, german, aired_count = [], 0, 0
    for n in numbers[:MAX_EPISODES]:
        ep, t = lib_eps.get(n, {}), titles.get(n, {})
        air = _air(ep) or t.get("air")
        de_title = t.get("de")
        label = f"E{n:02d}" + (f" „{de_title}“" if de_title else (f" {ep.get('title') or t.get('en')}" if (ep.get("title") or t.get("en")) else ""))
        efile = ep.get("episodeFile") if ep.get("hasFile") else None
        check = file_check(efile) if efile else None
        langs = file_langs(efile) if efile else []
        bad_name = " (heißt Deutsch, Tonspur aber nicht)" if check == "mislabelled" else ""
        if air and air > today:
            status = f"⏳ kommt erst am {_d(air)} (Original)"
        elif check == "german":
            status, german = "✅ bei dir auf Deutsch", german + 1
        elif german_rel.get(n):
            status, german = f"⬇️ deutscher Download da ({german_rel[n]})", german + 1
            if langs:
                status += f" – bei dir nur {', '.join(langs)}{bad_name}"
        elif langs:
            status = f"📁 bei dir nur auf {', '.join(langs)}{bad_name}"
        elif indexer and lib_id and any_rel.get(n):
            status = "🇺🇸 nur Originalfassung gefunden"
        elif de_title:
            status = "🔤 deutscher Titel bekannt – deutsche Ausstrahlung wahrscheinlich"
        else:
            status = "❌ noch nichts auf Deutsch"
        if not (air and air > today):
            aired_count += 1
        lines.append(f"{label}\n   {status}")
    if len(numbers) > MAX_EPISODES:
        lines.append(f"… und {len(numbers) - MAX_EPISODES} weitere Folgen")

    head = [f"🇩🇪 **{series.get('title')}** – Staffel {season}"]
    if lib_id and indexer:
        head.append(f"Auf Deutsch bestätigt: **{german} von {aired_count}** ausgestrahlten Folgen")
    elif aired_count:
        head.append(f"{aired_count} Folgen im Original ausgestrahlt")
    out = head + [""] + lines
    out.append(indexer_note.rstrip() if indexer_note else "")
    out.append("Quellen: deine Bibliothek" + (", deine Indexer" if lib_id and indexer else "")
               + (", TMDB (deutsche Folgentitel, Sendetermine)" if titles else ""))
    return "\n".join(x for x in out if x is not None).replace("\n\n\n", "\n\n")


async def movie_report(radarr: Any, tmdb: TMDB | None, tmdb_id: int, indexer: bool, today: date) -> str:
    movie = await radarr.lookup_tmdb(tmdb_id)
    title = f"{movie.get('title')} ({movie.get('year')})" if movie.get("year") else movie.get("title", "?")
    out = []
    if tmdb:
        info = await tmdb.german_info("movie", tmdb_id)
        title = f"{info.title_de or movie.get('title')} ({movie.get('year')})" if movie.get("year") else (info.title_de or title)
        out.append(f"🇩🇪 **{title}** auf Deutsch")
        if info.german_original:
            out.append("Deutscher Film – Originalsprache Deutsch.")

        def when(label: str, d: date | None) -> str:
            if not d:
                return f"{label}: kein Termin bekannt"
            return f"{label}: {_d(d)} " + ("✅" if d <= today else "⏳")

        out += [when("🎬 Kino", info.cinema), when("💾 Digital", info.digital), when("📀 Disc", info.disc)]
        if info.streaming:
            out.append(f"📺 Streaming: {', '.join(info.streaming[:4])}")
        if info.buy_rent:
            out.append(f"🛒 Kaufen/Leihen: {', '.join(info.buy_rent[:4])}")
        home = min(filter(None, (info.digital, info.disc)), default=None)
        if home and home <= today or info.streaming:
            verdict = "✅ Ja, eine deutsche Fassung gibt es zum Streamen/Kaufen."
        elif home:
            verdict = f"⏳ Noch nicht – zu Hause auf Deutsch ab {_d(home)}."
        elif info.cinema and info.cinema <= today:
            verdict = "🎬 Bisher nur im Kino auf Deutsch, noch kein Termin für digital/Disc."
        elif info.cinema:
            verdict = f"⏳ Noch nicht – deutscher Kinostart am {_d(info.cinema)}."
        else:
            verdict = "❌ Für Deutschland ist noch keine Veröffentlichung bekannt."
        out.insert(1, verdict)
    else:
        out.append(f"🇩🇪 **{title}**")

    lib_id = movie.get("id")
    if lib_id:
        full = await radarr.get(lib_id)
        mfile = full.get("movieFile") if full.get("hasFile") else None
        if mfile:
            check, langs = file_check(mfile), file_langs(mfile)
            if check == "german":
                out.append("✅ Bei dir vorhanden auf Deutsch")
            elif check == "mislabelled":
                out.append(f"🏷 Deine Datei heißt Deutsch, die Tonspur ist aber {langs_de(langs)}")
            else:
                out.append(f"📁 Bei dir nur auf {langs_de(langs)}")
        if indexer:
            try:
                rel = await radarr.releases(movieId=lib_id)
                ger = sum(1 for r in rel if _is_german_release(r))
                out.append(f"🔎 Indexer: {len(rel)} Downloads, davon {ger} auf Deutsch")
            except ArrError as e:
                out.append(f"⚠️ Indexer-Suche fehlgeschlagen: {e}")
    out.append("Quellen: TMDB (Termine für Deutschland, Streaming-Daten von JustWatch)"
               + (", deine Bibliothek" if lib_id else "") + (", deine Indexer" if lib_id and indexer else ""))
    return "\n".join(out)
