"""Tools exposed to the language model, and the button flows that run without it.

Token budget matters: the AI only finds the title. Picking a candidate, seasons and
quality happens through buttons handled here in code, without further AI calls.
"""

from __future__ import annotations

import asyncio
from builtins import max as builtins_max
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from . import debuglog, approval, collections_plex, dupes, german, manage, mediamgr, plex, profiles, qprofiles, recommend, sitewatch, together, urlinfo, watch, wishlist
from . import recipes, watchask
from . import users as users_mod
from .arr import ArrError, Radarr, Sonarr
from .llm import ToolCall, ToolResult, ToolSpec
from .state import Store, utcnow
from .tmdb import TMDB, TmdbError, german_summary

log = logging.getLogger(__name__)

MAX_RESULTS = 5
MAX_SEASON_BUTTONS = 10
MAX_SUGGESTIONS = 6
MAX_TOP = 10
NO_TMDB = 'ℹ️ Dafür brauche ich einen kostenlosen TMDB-Key. Der Admin trägt ihn in der docker-compose.yml als TMDB_API_KEY ein (themoviedb.org → Einstellungen → API).'
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
          "Oktober", "November", "Dezember"]


def build_specs(has_radarr: bool, has_sonarr: bool, has_tmdb: bool = False) -> list[ToolSpec]:
    obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
    profile = {"type": "string", "description": "Nur wenn der Nutzer eine Qualität nennt"}
    specs: list[ToolSpec] = []
    if has_radarr:
        specs += [
            ToolSpec(
                "search_movies",
                "Filmsuche. query: Titel oder 'imdb:tt…'/'tmdb:…'.",
                obj({"query": {"type": "string"}, "year": {"type": "integer"}}, ["query"]),
            ),
            ToolSpec(
                "add_movie",
                "Film hinzufügen und Download suchen. Bot fragt Qualität selbst.",
                obj({"tmdb_id": {"type": "integer"}, "quality_profile": profile}, ["tmdb_id"]),
            ),
        ]
    if has_sonarr:
        specs += [
            ToolSpec(
                "search_series",
                "Seriensuche. query: Titel oder 'tvdb:…'/'imdb:tt…'.",
                obj({"query": {"type": "string"}, "year": {"type": "integer"}}, ["query"]),
            ),
            ToolSpec(
                "add_series",
                "Serie hinzufügen und Download suchen. Ohne monitor/seasons fragt der Bot die Staffeln selbst.",
                obj(
                    {
                        "tvdb_id": {"type": "integer"},
                        "monitor": {"type": "string", "enum": ["all", "future"]},
                        "seasons": {"type": "array", "items": {"type": "integer"}},
                        "quality_profile": profile,
                    },
                    ["tvdb_id"],
                ),
            ),
        ]
    specs += [
        ToolSpec(
            "offer_titles",
            "Zeigt mehrere Suchtreffer als Buttons zur Auswahl; der Bot erledigt den Rest.",
            obj(
                {
                    "kind": {"type": "string", "enum": ["movie", "series"]},
                    "ids": {"type": "array", "items": {"type": "integer"}, "description": "tmdb- bzw. tvdb-IDs"},
                },
                ["kind", "ids"],
            ),
        ),
        ToolSpec(
            "search_existing",
            "Sucht einen Titel, der schon in Radarr/Sonarr ist, nochmal (Serie: alle fehlenden, schon erschienenen "
            "Folgen; schaltet Überwachung ein). title reicht (Original- oder deutscher Titel), lib_id falls bekannt.",
            obj(
                {
                    "kind": {"type": "string", "enum": ["movie", "series"]},
                    "title": {"type": "string"},
                    "lib_id": {"type": "integer"},
                    "seasons": {"type": "array", "items": {"type": "integer"}, "description": "nur diese Staffeln"},
                },
                ["kind"],
            ),
        ),
        ToolSpec(
            "german_fix",
            "Prüft die echte Tonspur (Serie: alle Folgen, Film: tmdb_id) auf Deutsch und bietet Ersatz per Button an.",
            obj({"tvdb_id": {"type": "integer"}, "tmdb_id": {"type": "integer"}, "season": {"type": "integer"}}),
        ),
        ToolSpec(
            "clean_episodes",
            "Folgen einer Serie löschen; Bot fragt per Button. which: not_german | season | all.",
            obj({"tvdb_id": {"type": "integer"}, "which": {"type": "string", "enum": ["not_german", "season", "all"]},
                 "season": {"type": "integer"}}, ["tvdb_id", "which"]),
        ),
        ToolSpec(
            "remove_title",
            "Titel aus der Bibliothek löschen; der Bot fragt per Button nach. lib_id aus der Suche.",
            obj({"kind": {"type": "string", "enum": ["movie", "series"]}, "lib_id": {"type": "integer"}},
                ["kind", "lib_id"]),
        ),
        ToolSpec(
            "pause_title",
            "Suche pausieren. days: danach automatisch wieder suchen; weglassen = bis resume_title.",
            obj({"kind": {"type": "string", "enum": ["movie", "series"]}, "lib_id": {"type": "integer"},
                 "days": {"type": "integer"}}, ["kind", "lib_id"]),
        ),
        ToolSpec(
            "resume_title",
            "Pausierte Suche sofort wieder starten.",
            obj({"kind": {"type": "string", "enum": ["movie", "series"]}, "lib_id": {"type": "integer"}},
                ["kind", "lib_id"]),
        ),
        ToolSpec(
            "list_quality_profiles",
            "Qualitätsprofile auflisten.",
            obj({"kind": {"type": "string", "enum": ["movie", "series"]}}, ["kind"]),
        ),
        ToolSpec(
            "download_status",
            "Zeigt dem Nutzer seine Downloads. title: nur dieser Titel (z. B. die letzte Suche). "
            "finished=true nur, wenn nach fertigen gefragt.",
            obj({"title": {"type": "string"}, "finished": {"type": "boolean"}}),
        ),
        ToolSpec(
            "suggest",
            "Vorschläge passend zum Geschmack des Nutzers; zeigt Buttons. from_year/to_year: Erscheinungsjahr "
            "(\"neuer als 2000\" = from_year 2001), min_rating 0-10. mood: Stimmung in seinen Worten "
            "(\"was Lustiges\", \"spannend\"), genres: deutsche Genres. like: \"wie X\" (Titel), dazu ggf. mood "
            "für \"wie X, aber lustiger\". max_minutes: \"kurz/unter 90 Minuten\" (Serien: pro Folge). "
            "german_only: nur auf Deutsch verfügbar. calibrate: \"lern meinen Geschmack\".",
            obj({"kind": {"type": "string", "enum": ["movie", "series"]}, "from_year": {"type": "integer"},
                 "to_year": {"type": "integer"}, "min_rating": {"type": "number"}, "mood": {"type": "string"},
                 "genres": {"type": "array", "items": {"type": "string"}}, "like": {"type": "string"},
                 "max_minutes": {"type": "integer"}, "german_only": {"type": "boolean"},
                 "calibrate": {"type": "boolean"}}),
        ),
        ToolSpec(
            "title_feedback",
            "Der Nutzer sagt, wie er einen Titel fand: \"X war super\" (liked), \"Y fand ich doof\" (disliked), "
            "\"Z kenne ich schon\" (seen). Verbessert die Vorschläge.",
            obj({"title": {"type": "string"}, "opinion": {"type": "string", "enum": ["liked", "disliked", "seen"]},
                 "kind": {"type": "string", "enum": ["movie", "series"]}}, ["title", "opinion"]),
        ),
        ToolSpec(
            "remember",
            "Merkt sich dauerhafte Vorlieben des Nutzers (\"merk dir\", \"ich mag keine …\", \"vergiss …\"). "
            "Genres deutsch. note nur für Wichtiges, max. 100 Zeichen.",
            obj({"like": {"type": "array", "items": {"type": "string"}},
                 "avoid": {"type": "array", "items": {"type": "string"}},
                 "from_year": {"type": "integer"}, "min_rating": {"type": "number"}, "note": {"type": "string"},
                 "forget": {"type": "array", "items": {"type": "string"}}}),
        ),
        ToolSpec(
            "calendar",
            "Was diese Woche/demnächst kommt (neue Folgen, Filmtermine). mine=true nur eigene Titel.",
            obj({"days": {"type": "integer"}, "mine": {"type": "boolean"}}),
        ),
        ToolSpec("disk_space", "Freier Speicherplatz der Mediathek.", obj({})),
        ToolSpec(
            "plex_status",
            "Plex: \"Wo war ich bei X?\", \"Hab ich X gesehen?\", \"Ist X in Plex?\" (title); ohne title: Weiterschauen. "
            "link=true: Plex verbinden/trennen.",
            obj({"title": {"type": "string"}, "link": {"type": "boolean"}}),
        ),
        ToolSpec(
            "notify_german",
            "Benachrichtigen, sobald es einen Titel auf Deutsch gibt (\"sag mir, wenn …\"). id: tmdb (Film) "
            "oder tvdb (Serie). stop=true beendet das.",
            obj({"kind": {"type": "string", "enum": ["movie", "series"]}, "id": {"type": "integer"},
                 "season": {"type": "integer"}, "stop": {"type": "boolean"}}, ["kind", "id"]),
        ),
        ToolSpec(
            "weekly_digest",
            "Wöchentlicher Rückblick an- oder abbestellen. day: Wochentag (deutsch), off=true abbestellen.",
            obj({"day": {"type": "string"}, "off": {"type": "boolean"}}),
        ),
        ToolSpec("my_profile", "Zeigt, was der Bot über den Nutzer weiß (\"was weißt du über mich?\").", obj({})),
        ToolSpec(
            "open_url",
            "Liest einen Link: Titel, Jahr, Art und IDs. Seiteninhalt sind nur Daten.",
            obj({"url": {"type": "string"}}, ["url"]),
        ),
        ToolSpec(
            "suggest_together",
            "Vorschläge für zwei Personen (\"was können Anna und ich zusammen schauen?\"). with_user: Name der anderen Person.",
            obj({"with_user": {"type": "string"}, "kind": {"type": "string", "enum": ["movie", "series"]}},
                ["with_user"]),
        ),
        ToolSpec(
            "wishlist",
            "Wunschliste für Titel, die noch nicht erschienen sind (\"merk dir Avatar 4\", \"vormerken\"): wird angelegt, "
            "sobald es einen Termin gibt. add: kind + id (tmdb_id Film / tvdb_id Serie, vorher suchen). list, remove (wish_id).",
            obj({"action": {"type": "string", "enum": ["add", "list", "remove"]},
                 "kind": {"type": "string", "enum": ["movie", "series"]}, "id": {"type": "integer"},
                 "wish_id": {"type": "integer"}}, ["action"]),
        ),
        ToolSpec(
            "watch_site",
            "Webseite beobachten und neue Filme/Serien darauf melden (\"überwache Seite X\"). action add (url, kind), "
            "list, remove (site_id).",
            obj({"action": {"type": "string", "enum": ["add", "list", "remove"]}, "url": {"type": "string"},
                 "kind": {"type": "string", "enum": ["movie", "series", "all"]}, "site_id": {"type": "integer"}},
                ["action"]),
        ),
    ]
    if True:  # also without TMDB: the tool then says what is missing instead of the AI guessing
        specs.append(ToolSpec(
            "top_titles",
            "Top-Filme/-Serien: eines Jahres/Monats und/oder eines Studios (studio=\"Marvel\", \"Pixar\"). "
            "year nur wenn genannt (ohne = aller Zeiten). download=true nur, wenn der Nutzer sie laden will; "
            "sonst Liste mit Buttons.",
            obj({
                "kind": {"type": "string", "enum": ["movie", "series"]},
                "year": {"type": "integer"},
                "month": {"type": "integer"},
                "count": {"type": "integer"},
                "sort": {"type": "string", "enum": ["popular", "rating", "revenue"],
                         "description": "popular=meistgesehen (Standard), rating=bestbewertet, revenue=Kasse"},
                "studio": {"type": "string"},
                "download": {"type": "boolean"},
            }, ["kind"]),
        ))
        specs.append(ToolSpec(
            "mute_notifications",
            "ALLE Download-Meldungen eine Zeit lang stumm (\"nerv mich 10 Minuten nicht\", \"3 Tage Ruhe\") oder "
            "wieder an (off). Danach kommt eine kurze Zusammenfassung. Dauer unklar: nachfragen.",
            obj({"minutes": {"type": "integer"}, "hours": {"type": "integer"}, "days": {"type": "integer"},
                 "off": {"type": "boolean", "description": "true = wieder laut"}}),
        ))
        specs.append(ToolSpec(
            "notification_settings",
            "WELCHE Download-Meldungen dieser Chat dauerhaft bekommt: start (Download gestartet), done (fertig; "
            "Serien nur eine Zusammenfassung am Ende), problem (fehlgeschlagen/hängt), nothing (nichts gefunden). "
            "Nur die genannten Arten setzen (true = an, false = aus); ohne Angaben: zeigt den Stand.",
            obj({k: {"type": "boolean"} for k in ("start", "done", "problem", "nothing")}),
        ))
        specs.append(ToolSpec(
            "bot_info",
            "Welche Version des Bots läuft und welches KI-Modell (\"welche Version?\", \"welches Modell nutzt du?\", "
            "\"bist du aktuell?\"). Zeigt es selbst an.",
            obj({}),
        ))
        specs.append(ToolSpec(
            "german_info",
            "Ob/wann es auf Deutsch gibt; zeigt dem Nutzer selbst eine Übersicht (Serie: pro Folge). "
            "id: tmdb (Film) bzw. tvdb (Serie). season nur wenn genannt. indexer=false nur bei Filmen ohne "
            "Frage nach Downloads.",
            obj({"kind": {"type": "string", "enum": ["movie", "series"]}, "id": {"type": "integer"},
                 "season": {"type": "integer"}, "indexer": {"type": "boolean"}}, ["kind", "id"]),
        ))
    return specs


def build_admin_specs() -> list[ToolSpec]:
    """Only offered to admins."""
    obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
    return [
        ToolSpec("admin_stats", "Statistik: Nutzer, Anfragen, Tokens, Kosten gesamt und pro Nutzer.", obj({})),
        ToolSpec("admin_users", "Nutzerliste mit Buttons zum Freigeben/Sperren.", obj({})),
        ToolSpec("admin_check", "Prüft alle APIs (KI, Radarr, Sonarr, SABnzbd, TMDB, Telegram).", obj({})),
        ToolSpec(
            "admin_quality_profile",
            "Neues Qualitätsprofil anlegen (Bot fragt vorher). Auflösung in p (4K=2160, 2K/FullHD=1080, HD=720). "
            "required_languages: alle Pflicht (\"Deutsch und Englisch\", \"Englisch\"), preferred: Bonus "
            "(\"+ Originalsprache\" = Original), only=true bei \"nur\"/\"only\". service weglassen = beide.",
            obj({"name": {"type": "string"}, "service": {"type": "string", "enum": ["radarr", "sonarr", "both"]},
                 "min_resolution": {"type": "integer"}, "max_resolution": {"type": "integer"},
                 "required_languages": {"type": "array", "items": {"type": "string"}},
                 "preferred_languages": {"type": "array", "items": {"type": "string"}},
                 "only": {"type": "boolean"}}, ["name"]),
        ),
        ToolSpec(
            "admin_media",
            "Mediathek verwalten: was fehlt, was nicht in Plex ist, was alle gesehen haben, was lange ungesehen "
            "herumliegt (zum Löschen). days: ab wie vielen Tagen 'lange'.",
            obj({"section": {"type": "string", "enum": ["overview", "missing", "not_in_plex", "watched", "unwatched"]},
                 "days": {"type": "integer"}}),
        ),
        ToolSpec(
            "admin_cleanup",
            "Aufräumregeln. action add: what=watched (von allen gesehen, days seit dem Anschauen) oder unwatched "
            "(nie angeschaut, days auf dem Server); kind movie/series/all; low_space_gb nur wenn Platz knapp; "
            "auto=true nur wenn der Admin ausdrücklich ohne Nachfrage löschen will. list, remove (rule_id), run (jetzt prüfen).",
            obj({"action": {"type": "string", "enum": ["add", "list", "remove", "run"]},
                 "what": {"type": "string", "enum": ["watched", "unwatched"]},
                 "kind": {"type": "string", "enum": ["movie", "series", "all"]},
                 "days": {"type": "integer"}, "low_space_gb": {"type": "integer"}, "auto": {"type": "boolean"},
                 "rule_id": {"type": "integer"}}, ["action"]),
        ),
        ToolSpec(
            "admin_delete_media",
            "Mediathek aufräumen AUF BEFEHL (\"lösche alle Filme, die länger als 180 Tage da sind und die ich nicht "
            "gesehen habe\", \"lösche alle gesehenen Filme\", \"die 10 größten ungesehenen Serien weg\"). Zeigt "
            "immer erst eine Liste mit Bestätigung. watched: true = ICH (Admin) habe es ganz gesehen, false = nie "
            "angefangen; older_days: länger als N Tage auf dem Server; seen_days: zuletzt vor über N Tagen geschaut; "
            "min_gb, max_rating (0-10), genres (deutsch), ended (nur beendete Serien), largest (nur die N größten). "
            "scope für Serien nur, wenn genannt (episodes | seasons | series) – sonst fragt der Bot per Button. "
            "Unklar, was gelöscht werden soll? Erst nachfragen.",
            obj({"kind": {"type": "string", "enum": ["movie", "series", "all"]}, "watched": {"type": "boolean"},
                 "older_days": {"type": "integer"}, "seen_days": {"type": "integer"}, "min_gb": {"type": "number"},
                 "max_rating": {"type": "number"}, "genres": {"type": "array", "items": {"type": "string"}},
                 "ended": {"type": "boolean"}, "scope": {"type": "string", "enum": ["episodes", "seasons", "series"]},
                 "largest": {"type": "integer"}}),
        ),
        ToolSpec(
            "admin_duplicates",
            "Doppelte Filme/Folgen in Plex finden (Inhalt: gleicher Titel/gleiche Folge und ähnliche Laufzeit) und "
            "die schlechtere Version löschen – erst Liste mit Bestätigung. action scan (Standard) oder settings: "
            "similarity (Prozent gleiche Laufzeit, Standard 97), languages (Reihenfolge, z. B. [\"de\",\"en\"]), "
            "resolutions (Text, z. B. \"1080p vor 4K\"), language_first (Sprache wichtiger als Auflösung). "
            "action list: nur anzeigen (\"zeig mir die Duplikate\") – ganze Liste mit Dateien, löschen nur per Button.",
            obj({"action": {"type": "string", "enum": ["scan", "list", "settings"]},
                 "kind": {"type": "string", "enum": ["movie", "series", "all"]},
                 "similarity": {"type": "number"}, "languages": {"type": "array", "items": {"type": "string"}},
                 "resolutions": {"type": "string"}, "language_first": {"type": "boolean"}}),
        ),
        ToolSpec(
            "admin_storage",
            "Speicher: wie viel frei ist, was am meisten Platz frisst, was weg könnte (gesehen / lange ungesehen).",
            obj({"top": {"type": "integer"}}),
        ),
        ToolSpec(
            "admin_keep",
            "Titel nie löschen (\"🛡 Dune behalten\", \"Breaking Bad nie löschen\") oder wieder freigeben (off). "
            "Ohne title: Liste der geschützten Titel.",
            obj({"title": {"type": "string"}, "kind": {"type": "string", "enum": ["movie", "series"]},
                 "off": {"type": "boolean"}}),
        ),
        ToolSpec(
            "admin_approval",
            "Freigabe-Regeln: Wünsche erst vom Admin freigeben lassen. action add: user (Name, leer = alle), "
            "kind movie/series/all, genres (z. B. [\"Horror\"]) nur bei diesen Genres, min_gb nur wenn der Download "
            "größer ist (Filme). list, remove (rule_id).",
            obj({"action": {"type": "string", "enum": ["add", "list", "remove"]}, "user": {"type": "string"},
                 "kind": {"type": "string", "enum": ["movie", "series", "all"]},
                 "genres": {"type": "array", "items": {"type": "string"}}, "min_gb": {"type": "number"},
                 "rule_id": {"type": "integer"}}, ["action"]),
        ),
        ToolSpec(
            "admin_plex_collection",
            "Plex-Sammlung anlegen/ergänzen: action create mit name und titles (alle gemeinten Filme/Serien mit Jahr, "
            "z. B. alle Marvel-Filme). auto_on/auto_off: automatische Sammlungen \"Wünsche von <Name>\" pro Nutzer.",
            obj({"action": {"type": "string", "enum": ["create", "auto_on", "auto_off"]}, "name": {"type": "string"},
                 "titles": {"type": "array", "items": {"type": "object", "properties": {
                     "title": {"type": "string"}, "year": {"type": "integer"}}, "required": ["title"]}}},
                ["action"]),
        ),
        ToolSpec(
            "admin_activity",
            "Was Nutzer gefragt haben, was geklappt hat, wo es Probleme gab. user: Name/ID (leer = alle).",
            obj({"user": {"type": "string"}, "problems_only": {"type": "boolean"}, "days": {"type": "integer"}}),
        ),
        ToolSpec(
            "admin_set_limit",
            "Limit setzen. who: Name/ID, 'standard' (alle Nutzer) oder 'gesamt' (ganzer Bot, nur KI-Limits). "
            "kind: KI-Nachrichten tag/monat, tokens/kosten (pro Monat), tokens_tag/kosten_tag (pro Tag) – oder Titel: filme_|serien_|staffeln_|folgen_ + "
            "stunde|tag|woche|monat (\"Sarah max. 3 Filme pro Tag\" = filme_tag 3). value: Zahl oder 'aus'.",
            obj({"who": {"type": "string"}, "kind": {"type": "string", "enum": sorted(users_mod.LIMIT_KEYS)},
                 "value": {"type": "string"}}, ["who", "kind", "value"]),
        ),
        ToolSpec(
            "admin_watch_questions",
            "Nachfragen zu Gesehenem: Hat der Admin einen Film / Staffeln in Plex gesehen, fragt der Bot hin und "
            "wieder, ob es weg kann. Ohne Angaben: Einstellung zeigen. on=false aus. every_days: alle N Tage, "
            "max: Fragen pro Runde, after_days: frühestens N Tage nach dem Anschauen. now=true: jetzt fragen.",
            obj({"on": {"type": "boolean"}, "every_days": {"type": "integer"}, "max": {"type": "integer"},
                 "after_days": {"type": "integer"}, "now": {"type": "boolean"}}),
        ),
        ToolSpec(
            "admin_limit_advice",
            "Empfiehlt Limits aus dem Verbrauch der letzten Tage (\"welches Limit sollte ich setzen?\", \"wie viele "
            "Tokens pro Tag sind sicher?\"): Gesamt Tokens/Kosten pro Tag und Monat, Standard pro Nutzer – mit "
            "Buttons zum Übernehmen.",
            obj({}),
        ),
        ToolSpec(
            "admin_permissions",
            "Einem Nutzer Rechte geben oder nehmen (\"Sarah darf ihre Filme und Serien löschen\" = grant "
            "delete_own). delete_own: nur selbst Angefragtes löschen; delete: alles löschen; pause: eigene Titel "
            "pausieren; repair: Deutsch-Reparatur/Folgen aufräumen; media: Medien-Manager (alles!); sites: "
            "Webseiten beobachten; no_limits: keine Limits. Ohne grant/revoke: zeigt die Rechte. Unklar, welches "
            "Recht gemeint ist (eigene oder alle)? Nachfragen.",
            obj({"who": {"type": "string"},
                 "grant": {"type": "array", "items": {"type": "string", "enum": list(users_mod.PERMS)}},
                 "revoke": {"type": "array", "items": {"type": "string", "enum": list(users_mod.PERMS)}}},
                ["who"]),
        ),
        ToolSpec(
            "admin_overview",
            "Verwaltungs-Übersicht (\"wie steht's?\", \"Übersicht\", \"Dashboard\"): Version, Modelle, Nutzer, "
            "Kosten, Offenes. Zeigt sie selbst an.",
            obj({}),
        ),
        ToolSpec(
            "admin_user_info",
            "Alles über einen Nutzer (\"zeig mir Anna\", \"was macht Bob?\"): Verbrauch, Limits, Qualität, Wünsche, "
            "Warnungen. Zeigt es selbst an.",
            obj({"who": {"type": "string"}}, ["who"]),
        ),
        ToolSpec(
            "build_tool",
            "Nur wenn KEIN Werkzeug die Frage genau beantwortet (auch nicht nach more_tools) und sie nur lesend ist "
            "(Listen, Top-Listen mit Filtern, Auswertungen der Bibliothek): eigenes Werkzeug bauen lassen. "
            "Der Admin sieht vorher die Kosten. request: die Frage in eigenen Worten.",
            obj({"request": {"type": "string"}}, ["request"]),
        ),
        ToolSpec(
            "admin_recipes",
            "Selbstgebaute Werkzeuge auflisten oder löschen (name).",
            obj({"action": {"type": "string", "enum": ["list", "delete"]}, "name": {"type": "string"}}, ["action"]),
        ),
        ToolSpec(
            "admin_max_quality",
            "Höchste Qualität, die Nutzer anfragen dürfen. who: Name/ID oder 'standard' (alle Nutzer). "
            "quality: SD, 720, 1080, 4K oder 'aus'.",
            obj({"who": {"type": "string"}, "quality": {"type": "string"}}, ["who", "quality"]),
        ),
        ToolSpec(
            "admin_user",
            "Nutzer freigeben, sperren oder entfernen.",
            obj({"who": {"type": "string"},
                 "action": {"type": "string", "enum": ["freigeben", "sperren", "entfernen"]}}, ["who", "action"]),
        ),
    ]


@dataclass
class View:
    """A message the bot renders in code (downloads, statistics, users, API check)."""

    chat_id: int
    kind: str  # downloads | stats | users | check | profile
    finished: bool = False
    query: str | None = None  # downloads: only titles containing this
    arg: Any = None  # calendar: (days, mine)


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", False, [], {})}


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------
# Button flows
# --------------------------------------------------------------------------
@dataclass
class Step:
    """What the bot shows next for a flow: a question with buttons, or a final text."""

    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (label, value)
    timeout: int = 0  # seconds until the default is taken (quality only)
    done: bool = False


@dataclass
class Candidate:
    kind: str  # "movie" | "series"
    ext_id: int  # tmdb (movie) or tvdb (series)
    title: str
    year: int | None
    seasons: list[int] = field(default_factory=list)
    lib_id: int | None = None
    has_file: bool = False
    rating: float | None = None  # set for suggestions
    tmdb_id: int | None = None  # suggestions: for "nichts davon"
    genres: list[str] = field(default_factory=list)  # suggestions: German genre names

    @property
    def label(self) -> str:
        s = ("🎬 " if self.kind == "movie" else "📺 ") if self.rating is not None else ""
        s += f"{self.title} ({self.year})" if self.year else self.title
        if self.kind == "series" and self.seasons:
            s += f" · {len(self.seasons)} St."
        if self.rating:
            s += f" · ⭐ {self.rating:.1f}".replace(".", ",")
        return s


@dataclass
class Flow:
    id: str
    chat_id: int
    cand: Candidate
    monitor: str | None = None
    seasons: list[int] | None = None
    profile: str | None = None
    stage: str = ""
    profiles: list[str] = field(default_factory=list)
    default_profile: str = ""
    message_id: int | None = None
    step: Step | None = None
    missing: list[int] = field(default_factory=list)  # extend stage: seasons not (fully) downloaded
    data: Any = None  # gfix / clean stage: the plan to run on confirmation


@dataclass
class Offer:
    id: str
    chat_id: int
    cands: list[Candidate]
    message_id: int | None = None
    text: str = "🔎 Welchen meinst du?"
    bulk: bool = False  # top lists: extra button "add all"
    suggest: bool = False  # suggestions: extra button "none of these" (learned)
    calibrate: bool = False  # quick start: 👍/👎 per title, then "Fertig"
    mode: str = ""  # "rate": buttons 👎/👁 per title instead of "add"
    hidden: list[int] = field(default_factory=list)  # titles already rated (button gone)


def DownloadsView(chat_id: int, finished: bool = False) -> "View":  # noqa: N802 - kept for readability
    return View(chat_id, "downloads", finished)



class Held(ArrError):
    """The wish waits for the admin's approval (not an error for the user)."""

class ToolRunner:
    def __init__(
        self,
        radarr: Radarr | None,
        sonarr: Sonarr | None,
        store: Store,
        min_availability: str,
        quality_prompt_seconds: int = 0,
        tmdb: TMDB | None = None,
        tz: tzinfo | None = None,
    ):
        self.tmdb = tmdb
        self.tz = tz or ZoneInfo("Europe/Berlin")
        # Called after something was added/searched: (chat_id, text). The bot tells the admin.
        self.on_added: Callable[[int, str], Awaitable[None]] | None = None
        self.is_admin: Callable[[int], bool] = lambda _uid: False  # set by the bot
        self.notify_user: Callable[[int, str], Awaitable[None]] | None = None
        self.radarr = radarr
        self.sonarr = sonarr
        self.store = store
        self.min_availability = min_availability
        self.quality_prompt_seconds = quality_prompt_seconds
        self.specs = build_specs(radarr is not None, sonarr is not None, tmdb is not None)
        self.flows: dict[str, Flow] = {}
        self.offers: dict[str, Offer] = {}
        self.rating_asks: dict[str, Any] = {}  # "wie fandest du X?" -> the title
        self._outbox: dict[int, list[Flow | Offer | View]] = {}
        self.users: Any = None  # UserStore, set by the bot (admin tools)
        self.admin_specs = build_admin_specs()
        self._seen: dict[int, dict[tuple[str, int], Candidate]] = {}
        self.url_options: dict = {}  # tests inject a fake transport here
        self.profiles: profiles.ProfileStore | None = None  # set by the bot
        self.plex: plex.Plex | None = None  # set by the bot
        self.plex_store: plex.PlexStore | None = None
        self.cleanup: Any = None  # cleanup.RuleStore, set by the bot
        self.sites: sitewatch.SiteStore | None = None  # set by the bot
        self.wishes: wishlist.WishStore | None = None  # set by the bot
        self.recipes: recipes.RecipeStore | None = None  # set by the bot
        self.bot_info: Callable[[int], Awaitable[str]] | None = None  # version and models, set by the bot
        self.builder: Any = None  # LLM provider that writes recipes (a stronger model)
        self.builder_model = ""
        self.builder_cost: Callable[[dict], float | None] = lambda _u: None
        self.on_llm_usage: Callable[[int, dict, float | None], None] | None = None  # bot books it
        self.site_hours = 6
        self.approvals: approval.ApprovalStore | None = None  # set by the bot
        self.on_approval: Callable[[approval.Pending], Awaitable[None]] | None = None  # bot asks the admins
        self.running: dict[int, tuple[str, float]] = {}  # chat -> (tool, since)
        self.trace: dict[int, list[tuple[str, str | None, str]]] = {}

    # ---- plumbing -------------------------------------------------------
    def take_outbox(self, chat_id: int) -> list[Flow | Offer | View]:
        """Button messages created during the last AI turn; the bot sends them."""
        return self._outbox.pop(chat_id, [])

    # Users only find and add titles; anything that changes or deletes what is there, costs extra or
    # watches in the background is for admins.
    ADMIN_ONLY = {"german_fix", "clean_episodes", "remove_title", "pause_title", "resume_title", "watch_site",
                  "build_tool"}

    def _admin(self, uid: int) -> bool:
        """Without user management (single-user setup) whoever may use the bot is the admin."""
        return self.users is None or self.is_admin(uid)

    # Tools a user may get with an extra right from the admin ("Sarah darf ihre Filme löschen").
    TOOL_PERMS = {"remove_title": ("delete", "delete_own"), "pause_title": ("pause",), "resume_title": ("pause",),
                  "german_fix": ("repair",), "clean_episodes": ("repair",), "watch_site": ("sites",),
                  "admin_delete_media": ("media",), "admin_storage": ("media",), "admin_duplicates": ("media",),
                  "admin_keep": ("media",)}

    def _admin_only(self, name: str) -> bool:
        return name in self.ADMIN_ONLY or name.startswith(("admin_", "recipe_"))

    def may(self, uid: int, name: str) -> bool:
        """May uid run this tool? Admins: everything. Users: search/add, plus what the admin allowed them."""
        if self._admin(uid):
            return True
        if self.users is None or not self.users.is_allowed(uid):
            return False
        perms = self.TOOL_PERMS.get(name)
        if perms:
            return any(self.users.has_perm(uid, p) for p in perms)
        return not self._admin_only(name)

    def specs_for(self, user_id: int) -> list[ToolSpec]:
        if self.users is not None and self.is_admin(user_id):
            return self.specs + self.admin_specs + self.recipe_specs()
        if self.users is None:
            return self.specs
        return [s for s in self.specs + self.admin_specs if self.may(user_id, s.name)]

    def recipe_specs(self) -> list[ToolSpec]:
        return [r.spec() for r in self.recipes.all()] if self.recipes else []

    # ---- quality limit per user ----------------------------------------------------------------
    def max_quality(self, uid: int) -> int:
        return self.users.max_quality(uid) if self.users is not None else 0

    async def allowed_profiles(self, kind: str, uid: int) -> list[dict]:
        profiles = await self._svc(kind).quality_profiles()
        cap = self.max_quality(uid)
        return [p for p in profiles if not cap or 0 < qprofiles.max_resolution(p) <= cap]

    async def _check_cap(self, kind: str, uid: int, profile: str) -> None:
        cap = self.max_quality(uid)
        if cap and profile not in [p["name"] for p in await self.allowed_profiles(kind, uid)]:
            allowed = ", ".join(p["name"] for p in await self.allowed_profiles(kind, uid)) or "keins"
            raise ArrError(f"„{profile}“ ist über deiner Grenze ({users_mod.quality_label(cap)}). Möglich: {allowed}")

    async def _capped_profile(self, flow: Flow) -> str | None:
        """The profile to add with: what was chosen, unless it is above the user's limit."""
        cap = self.max_quality(flow.chat_id)
        if not cap:
            return flow.profile
        svc = self._svc(flow.cand.kind)
        chosen = await svc.resolve_profile(flow.profile)
        if 0 < qprofiles.max_resolution(chosen) <= cap:
            return chosen["name"]
        allowed = await self.allowed_profiles(flow.cand.kind, flow.chat_id)
        if not allowed:
            raise ArrError(f"Es gibt kein Qualitätsprofil bis {users_mod.quality_label(cap)} – sag dem Admin Bescheid.")
        if flow.profile:  # asked for more than allowed
            raise ArrError(f"„{chosen['name']}“ ist über deiner Grenze ({users_mod.quality_label(cap)}). "
                           f"Möglich: {', '.join(p['name'] for p in allowed)}")
        return max(allowed, key=qprofiles.max_resolution)["name"]

    # ---- buttons that survive a restart ----------------------------------------------------------
    KEEP_STAGES = ("", "seasons", "quality", "extend")  # flows with a plan (gfix, clean ...) are rebuilt on demand
    KEEP_MAX = 300

    def dump_buttons(self) -> dict:
        flows = [asdict(f) for f in self.flows.values() if f.stage in self.KEEP_STAGES and f.data is None]
        offers = [asdict(o) for o in self.offers.values()]
        return {"flows": flows[-self.KEEP_MAX:], "offers": offers[-self.KEEP_MAX:]}

    def load_buttons(self, data: dict) -> int:
        n = 0
        for f in data.get("flows") or []:
            try:
                f = dict(f)
                cand = Candidate(**f.pop("cand"))
                step = f.pop("step", None)
                flow = Flow(cand=cand, step=Step(**{**step, "buttons": [tuple(b) for b in step["buttons"]]})
                            if step else None, **f)
                self.flows.setdefault(flow.id, flow)
                n += 1
            except (TypeError, KeyError) as e:
                log.info("Button-Zustand übersprungen: %s", e)
        for o in data.get("offers") or []:
            try:
                o = dict(o)
                offer = Offer(cands=[Candidate(**c) for c in o.pop("cands")], **o)
                self.offers.setdefault(offer.id, offer)
                n += 1
            except (TypeError, KeyError) as e:
                log.info("Button-Zustand übersprungen: %s", e)
        return n

    def take_trace(self, chat_id: int) -> list[tuple[str, str | None, str]]:
        """(tool, error or None, final text) of every tool run since the last call (activity log)."""
        return self.trace.pop(chat_id, [])

    async def run(self, call: ToolCall, chat_id: int) -> ToolResult:
        self.running[chat_id] = (call.name, asyncio.get_running_loop().time())  # for "arbeitest du noch?"
        try:
            r = await self._run(call, chat_id)
        finally:
            self.running.pop(chat_id, None)
        error = r.content if r.is_error else (r.final if (r.final or "").startswith("⚠️") else None)
        self.trace.setdefault(chat_id, []).append((call.name, error, r.final or ""))
        return r

    async def _run(self, call: ToolCall, chat_id: int) -> ToolResult:
        fn = getattr(self, f"_t_{call.name}", None)
        if fn is None and call.name.startswith("recipe_") and self.recipes and self.recipes.get(call.name):
            name = call.name

            async def fn(chat_id: int, **args: Any) -> Final:
                return await self.run_recipe(chat_id, name, args)
        if fn is None:
            return ToolResult(call.id, call.name, f"Unbekanntes Werkzeug {call.name}", is_error=True)
        if not self.may(chat_id, call.name):
            if call.name.startswith("admin_") and call.name not in self.TOOL_PERMS:
                return ToolResult(call.id, call.name, "Nur für Admins.", is_error=True)
            return ToolResult(call.id, call.name, "Das darf nur der Admin. Du kannst Filme und Serien suchen und "
                              "hinzufügen – für alles andere frag den Admin.", is_error=True)
        try:
            result = await fn(chat_id=chat_id, **call.args)
        except TmdbError as e:
            # Shown as is: the AI would only paraphrase it into "keine Daten gefunden".
            log.warning("TMDB bei %s: %s", call.name, e)
            return ToolResult(call.id, call.name, "ok", final=f"⚠️ {e}")
        except ArrError as e:
            return ToolResult(call.id, call.name, str(e), is_error=True)
        except TypeError as e:
            return ToolResult(call.id, call.name, f"Ungültige Parameter: {e}", is_error=True)
        except Exception as e:  # noqa: BLE001 - report anything to the model instead of crashing
            log.exception("Tool %s fehlgeschlagen", call.name)
            return ToolResult(call.id, call.name, f"Interner Fehler: {e}", is_error=True)
        if isinstance(result, Final):
            return ToolResult(call.id, call.name, "ok", final=result.text)
        return ToolResult(call.id, call.name, _dump(result))

    def _need(self, svc: Any, name: str) -> Any:
        if svc is None:
            raise ArrError(f"{name} ist nicht konfiguriert.")
        return svc

    def _svc(self, kind: str) -> Any:
        return self._need(self.radarr, "Radarr") if kind == "movie" else self._need(self.sonarr, "Sonarr")

    def _remember(self, chat_id: int, c: Candidate) -> None:
        seen = self._seen.setdefault(chat_id, {})
        seen[(c.kind, c.ext_id)] = c
        while len(seen) > 100:
            seen.pop(next(iter(seen)))

    async def _candidate(self, chat_id: int, kind: str, ext_id: int) -> Candidate:
        c = self._seen.get(chat_id, {}).get((kind, ext_id))
        if c:
            return c
        if kind == "movie":
            c = _movie_cand(await self._need(self.radarr, "Radarr").lookup_tmdb(ext_id))
        else:
            c = _series_cand(await self._need(self.sonarr, "Sonarr").lookup_tvdb(ext_id))
        self._remember(chat_id, c)
        return c

    # ---- flows ------------------------------------------------------------
    async def _start_flow(self, chat_id: int, cand: Candidate, **choices: Any) -> Flow:
        flow = Flow(uuid.uuid4().hex[:10], chat_id, cand, **choices)
        self.flows[flow.id] = flow
        flow.step = await self._advance(flow)
        return flow

    async def _advance(self, flow: Flow) -> Step:
        c = flow.cand
        if flow.stage == "extend":
            return await self._extend_step(flow)
        if c.kind == "series" and flow.monitor is None and not flow.seasons:
            if not c.seasons and self.sonarr and c.ext_id:
                # Suggestions and top lists come from TMDB without seasons: ask Sonarr, else it'd be "all".
                try:
                    c.seasons = _series_cand(await self.sonarr.lookup_tvdb(int(c.ext_id))).seasons
                except ArrError as e:
                    log.info("Staffeln von %s nicht gefunden: %s", c.label, e)
            if len(c.seasons) > 1:
                flow.stage = "seasons"
                return Step(f"📺 Welche Staffeln von {c.label}?", _season_buttons(c.seasons))
            flow.monitor = "all"

        if flow.profile is None and self.quality_prompt_seconds > 0:
            profiles = [p["name"] for p in await self.allowed_profiles(c.kind, flow.chat_id)]
            if len(profiles) > 1:
                flow.stage = "quality"
                flow.profiles = profiles
                flow.default_profile = (await self._capped_profile(flow)
                                        or (await self._svc(c.kind).resolve_profile(None))["name"])
                if flow.default_profile not in profiles:
                    flow.default_profile = profiles[0]
                return Step(
                    f"🎚 Welche Qualität für {_what(flow)}?\n"
                    f"Ohne Auswahl nehme ich in {self.quality_prompt_seconds} s „{flow.default_profile}“.",
                    [(("⭐ " if p == flow.default_profile else "") + p, f"p{i}") for i, p in enumerate(profiles)],
                    timeout=self.quality_prompt_seconds,
                )

        flow.stage = "done"
        self.flows.pop(flow.id, None)
        try:
            await self._add(flow)
        except Held as e:
            return Step(f"⏳ {_what(flow)}: {e}", done=True)
        except ArrError as e:
            return Step(f"❌ {c.label} konnte nicht hinzugefügt werden: {e}", done=True)
        except Exception as e:  # noqa: BLE001
            log.exception("Hinzufügen fehlgeschlagen")
            return Step(f"❌ {c.label} konnte nicht hinzugefügt werden: {e}", done=True)
        prof = f" in „{flow.profile}“" if flow.profile else ""
        text = f"✅ {_what(flow)} hinzugefügt{prof}. Ich melde mich, wenn der Download startet."
        german = await self._german_line(c.kind, c.ext_id)
        return Step(text + (f"\n{german}" if german else ""), done=True)

    async def _tmdb_id(self, kind: str, ext_id: int) -> int | None:
        if kind == "movie":
            return ext_id
        return await self.tmdb.tv_from_tvdb(ext_id) if self.tmdb else None

    async def _german_line(self, kind: str, ext_id: int) -> str | None:
        """Warn when there is no German version yet (TMDB), e.g. 'Deutsch erst ab 12.03.'."""
        if not self.tmdb:
            return None
        try:
            tid = await self._tmdb_id(kind, ext_id)
            return german_summary(await self.tmdb.german_info(kind, tid), date.today()) if tid else None
        except TmdbError as e:
            log.info("TMDB: %s", e)
            return None

    # ---- extend a series that is already in Sonarr -----------------------------
    async def _start_extend(self, chat_id: int, cand: Candidate) -> Flow | Step:
        series = await self._need(self.sonarr, "Sonarr").get(cand.lib_id)
        have, missing = _season_overview(series)
        name = cand.title
        if not missing:
            more = " Neue Folgen kommen automatisch dazu." if series.get("status") != "ended" else ""
            return Step(f"👍 {name} ist komplett vorhanden ({_seasons_text(have) or 'alle Staffeln'}).{more}", done=True)
        flow = Flow(uuid.uuid4().hex[:10], chat_id, cand, stage="extend", missing=missing)
        self.flows[flow.id] = flow
        have_txt = f"Du hast {_seasons_text(have)}. " if have else ""
        buttons = [(f"⬇️ Alle fehlenden laden ({_seasons_text(missing, short=True)})", "rest")]
        if len(missing) > 1:
            buttons += [(f"Nur Staffel {n}", f"s{n}") for n in missing[:MAX_SEASON_BUTTONS]]
        buttons.append(("Nein, danke", "no"))
        flow.step = Step(
            f"📺 {name} ist schon da. {have_txt}Es fehlen {_seasons_text(missing)}.\nSoll ich sie laden?", buttons
        )
        return flow

    async def _extend_step(self, flow: Flow) -> Step:
        self.flows.pop(flow.id, None)
        if flow.seasons is None:
            return Step(f"👍 Alles klar, {flow.cand.title} bleibt wie es ist.", done=True)
        final = await self._search_existing(flow.chat_id, "series", flow.cand.lib_id, flow.seasons)
        return Step(final.text, done=True)

    async def _hold_rule(self, flow: Flow) -> approval.Rule | None:
        """The approval rule that applies to this wish (genres checked here, size later)."""
        if self.approvals is None or self.is_admin(flow.chat_id):
            return None
        c = flow.cand
        rules = self.approvals.applies(flow.chat_id, c.kind)
        if not rules:
            return None
        genres = list(c.genres)
        if not genres and any(r.genres for r in rules):
            try:
                found = await (self._need(self.radarr, "Radarr").lookup_tmdb(c.ext_id) if c.kind == "movie"
                               else self._need(self.sonarr, "Sonarr").lookup_tvdb(c.ext_id))
                genres = found.get("genres") or []
            except ArrError:
                genres = []
        hits = [r for r in rules if r.matches(flow.chat_id, c.kind, genres)]
        # A rule without size holds everything; otherwise the smallest size limit decides.
        return next((r for r in hits if not r.min_gb), None) or min(hits, key=lambda r: r.min_gb, default=None)

    async def _hold(self, flow: Flow, reason: str, **extra: Any) -> None:
        c = flow.cand
        p = self.approvals.hold(approval.Pending(  # type: ignore[union-attr]
            "", flow.chat_id, c.kind, c.ext_id, _what(flow), reason, flow.monitor, flow.seasons, flow.profile, **extra))
        if self.on_approval:
            await self.on_approval(p)
        raise Held("wartet auf Freigabe durch den Admin. Ich sage dir Bescheid.")

    def _wants_limit(self, uid: int, what: str) -> bool:
        if self.users is None:
            return False
        return any(k.startswith(what + "_") for k in self.users.limits_for(uid))

    async def _check_title_limit(self, uid: int, **counts: int) -> None:
        if self.users is None:
            return
        reason = self.users.title_limit(uid, **counts)
        if reason:
            raise ArrError(f"⛔ {reason}")

    async def _series_counts(self, flow: Flow) -> dict:
        c = flow.cand
        seasons = flow.seasons or [s for s in c.seasons if s > 0]
        counts = {"serien": 1, "staffeln": len(seasons) or 1}
        if self._wants_limit(flow.chat_id, "folgen") and self.tmdb:
            try:
                tmdb_id = c.tmdb_id or (await self._need(self.sonarr, "Sonarr").lookup_tvdb(c.ext_id)).get("tmdbId")
                per = await self.tmdb.season_episodes(int(tmdb_id)) if tmdb_id else {}
            except (ArrError, TmdbError, ValueError):
                per = {}
            counts["folgen"] = sum(n for s, n in per.items() if s in seasons) if seasons else sum(
                n for s, n in per.items() if s > 0)
        return counts

    async def _add(self, flow: Flow, approved: bool = False) -> None:
        c = flow.cand
        counts = {"filme": 1} if c.kind == "movie" else await self._series_counts(flow)
        if not approved:  # the admin said yes: no limit
            await self._check_title_limit(flow.chat_id, **counts)
        flow.profile = await self._capped_profile(flow)
        rule = None if approved else await self._hold_rule(flow)
        if rule and not rule.min_gb:
            await self._hold(flow, f"Genre {' / '.join(rule.genres)}" if rule.genres else "Freigabe nötig")
        if c.kind == "movie":
            radarr = self._need(self.radarr, "Radarr")
            movie = await radarr.add(c.ext_id, flow.profile, self.min_availability, search=rule is None)
            title = f"{c.title or movie['title']} ({movie.get('year')})"
            if rule:  # size rule: look at the release Radarr would take
                try:
                    rel = sorted((r for r in await radarr.releases(movieId=movie["id"]) if manage._usable_any(r)),
                                 key=manage._score, reverse=True)
                except ArrError:
                    rel = []
                size = (rel[0].get("size") or 0) / 2**30 if rel else 0
                if size > (rule.min_gb or 0):
                    await radarr.set_monitored(movie["id"], False)  # Radarr must not grab it by itself
                    await self._hold(flow, f"Download {size:.1f} GB (Grenze {rule.min_gb:g} GB)".replace(".", ","),
                                     arr_id=movie["id"], release=rel[0])
                await radarr.search(movie["id"])
            self.store.track("radarr", movie["id"], title, flow.chat_id)
            self.store.record_request(flow.chat_id, "movie", title, c.ext_id)
            self._learn(flow.chat_id, "requested", "movie", movie, title)
        else:
            series = await self._need(self.sonarr, "Sonarr").add(
                c.ext_id, flow.profile, monitor=flow.monitor or "all", seasons=flow.seasons
            )
            title = c.title or series["title"]
            self.store.track("sonarr", series["id"], title, flow.chat_id)
            self.store.record_request(flow.chat_id, "series", title, series.get("tmdbId"), c.ext_id)
            self._learn(flow.chat_id, "requested", "series", series, title)
        if self.users is not None:
            self.users.record_adds(flow.chat_id, **counts)
        await self._added(flow.chat_id, f"➕ {_what(flow)}")

    async def decide(self, pid: str, ok: bool) -> tuple[approval.Pending | None, str]:
        """Admin pressed Freigeben/Ablehnen. Returns (wish, text for the admin); the user is told here."""
        p = self.approvals.take(pid) if self.approvals else None
        if p is None:
            return None, "Schon erledigt."
        radarr = self.radarr
        try:
            if not ok:
                if p.arr_id and radarr:
                    await radarr.delete(p.arr_id, delete_files=False)
                note = f"🙅 {p.title}: Der Admin hat den Wunsch abgelehnt."
            elif p.arr_id and radarr:  # size check: movie is there, grab the held release
                await radarr.set_monitored(p.arr_id, True)
                try:
                    await radarr.grab(p.release or {})
                except (ArrError, KeyError):
                    await radarr.search(p.arr_id)
                self.store.track("radarr", p.arr_id, p.title, p.user_id)
                self.store.record_request(p.user_id, "movie", p.title, p.ext_id)
                note = f"✅ {p.title}: freigegeben, der Download startet."
            else:
                c = Candidate(p.kind, p.ext_id, p.title, None)
                await self._add(Flow(uuid.uuid4().hex[:10], p.user_id, c, p.monitor, p.seasons, p.profile), approved=True)
                note = f"✅ {p.title}: freigegeben und hinzugefügt."
        except ArrError as e:
            return p, f"❌ {p.title}: {e}"
        if self.notify_user:
            await self.notify_user(p.user_id, note)
        return p, ("✅ Freigegeben: " if ok else "🙅 Abgelehnt: ") + p.title

    async def _added(self, chat_id: int, text: str) -> None:
        if self.on_added:
            try:
                await self.on_added(chat_id, text)
            except Exception:  # noqa: BLE001
                log.exception("on_added fehlgeschlagen")

    async def choose(self, flow_id: str, value: str) -> Flow | None:
        """A button of a flow was pressed. Returns the flow with its next step, or None if stale."""
        flow = self.flows.get(flow_id)
        if flow is None:
            return None
        token = debuglog.LOG.begin(flow.chat_id, f"Button „{value}“ bei {flow.cand.title} ({flow.stage})", "🔘")
        try:
            out = await self._choose(flow, value)
        except Exception as e:
            debuglog.LOG.add("💥", f"{type(e).__name__}: {e}")
            raise
        finally:
            debuglog.LOG.end(token)
        if out is not None and out.step:
            debuglog.LOG.add("📤", out.step.text, flow.chat_id)
        return out

    async def _choose(self, flow: Flow, value: str) -> Flow | None:
        if flow.stage == "remove":
            self.flows.pop(flow.id, None)
            flow.step = await self._do_remove(flow, value)
            return flow
        if flow.stage == "recipe":
            self.flows.pop(flow.id, None)
            flow.step = await self._recipe_step(flow, value)
            if not flow.step.done:
                self.flows[flow.id] = flow
            return flow
        if flow.stage == "wa":
            flow.step = await self._wa_step(flow, value)
            return flow
        if flow.stage == "adv":
            from . import limitadvice

            self.flows.pop(flow.id, None)
            flow.step = Step("👍 Limits bleiben wie sie sind." if value == "no"
                             else limitadvice.apply(flow.data, self.users, value), done=True)
            return flow
        if flow.stage == "dup":
            self.flows.pop(flow.id, None)
            flow.step = await self._dup_step(flow, value)
            if not flow.step.done:
                self.flows[flow.id] = flow
            return flow
        if flow.stage == "mm":
            self.flows.pop(flow.id, None)
            flow.step = await self._mm_step(flow, value)
            if not flow.step.done:
                self.flows[flow.id] = flow
            return flow
        if flow.stage == "qprof":
            self.flows.pop(flow.id, None)
            flow.step = await self._create_profiles(flow, value)
            return flow
        if flow.stage in ("gfix", "clean", "mfix"):
            self.flows.pop(flow.id, None)
            flow.step = await self._run_series_job(flow, value)
            if not flow.step.done:  # "Weiter suchen", "Neu einlesen": the buttons stay
                self.flows[flow.id] = flow
            return flow
        if flow.stage == "extend":
            if value == "rest":
                flow.seasons = flow.missing
            elif value.startswith("s") and value[1:].isdigit():
                flow.seasons = [int(value[1:])]
            elif value != "no":
                return flow
        elif flow.stage == "seasons":
            if value in ("all", "future"):
                flow.monitor = value
            elif value.startswith("s") and value[1:].isdigit():
                flow.seasons = [int(value[1:])]
            else:
                return flow
        elif flow.stage == "quality":
            if not (value.startswith("p") and value[1:].isdigit() and int(value[1:]) < len(flow.profiles)):
                return flow
            flow.profile = flow.profiles[int(value[1:])]
        flow.step = await self._advance(flow)
        return flow

    async def quality_timeout(self, flow_id: str) -> Flow | None:
        flow = self.flows.get(flow_id)
        if flow is None or flow.stage != "quality":
            return None
        return await self.choose(flow_id, f"p{flow.profiles.index(flow.default_profile)}")

    async def add_all(self, offer_id: str) -> str | None:
        """'Alle hinzufügen' under a top list: add every offered title with the default quality."""
        offer = self.offers.pop(offer_id, None)
        if offer is None:
            return None
        lines = []
        for c in offer.cands:
            name = c.title + (f" ({c.year})" if c.year else "")
            try:
                await self._add(Flow(uuid.uuid4().hex[:10], offer.chat_id, c, monitor="all"))
                lines.append(f"✅ {name}")
            except Held:
                lines.append(f"⏳ {name} – wartet auf Freigabe")
            except ArrError as e:
                lines.append(f"❌ {name} – {e}")
        lines.append("\nQualität: Standardprofil. Ich melde mich, wenn Downloads starten.")
        return "\n".join(lines)

    async def choose_offer(self, offer_id: str, idx: int) -> tuple[Offer, Flow | Step] | None:
        """A candidate button was pressed: start its flow (or re-search a library title)."""
        offer = self.offers.pop(offer_id, None)
        if offer is None or not 0 <= idx < len(offer.cands):
            return None
        c = offer.cands[idx]
        if c.lib_id and c.kind == "series":
            res = await self._start_extend(offer.chat_id, c)
            if isinstance(res, Flow):
                res.message_id = offer.message_id
            return offer, res
        if c.lib_id:
            if c.has_file:
                return offer, Step(f"👍 {c.label} ist schon vorhanden.", done=True)
            final = await self._search_existing(offer.chat_id, c.kind, c.lib_id, None)
            return offer, Step(final.text, done=True)
        flow = await self._start_flow(offer.chat_id, c)
        flow.message_id = offer.message_id
        return offer, flow

    # ---- tools -------------------------------------------------------------
    async def _t_search_movies(self, chat_id: int, query: str, year: int | None = None) -> list[dict]:
        res = await self._need(self.radarr, "Radarr").lookup(query)
        res = _year_filter(res, year)[:MAX_RESULTS]
        de = await self._german_titles("movie", [m.get("tmdbId") for m in res])
        out = []
        for m in res:
            c = _movie_cand(m)
            c.title = de.get(c.ext_id) or c.title
            self._remember(chat_id, c)
            out.append(_compact({
                "tmdb": c.ext_id, "title": c.title, "year": c.year,
                "orig": m.get("originalTitle") if m.get("originalTitle") not in (c.title, None) else None,
                "lib_id": c.lib_id, "file": c.has_file,
            }))
        return out

    async def _t_search_series(self, chat_id: int, query: str, year: int | None = None) -> list[dict]:
        res = await self._need(self.sonarr, "Sonarr").lookup(query)
        res = _year_filter(res, year)[:MAX_RESULTS]
        de = await self._german_titles("series", [s.get("tmdbId") for s in res])
        out = []
        for s in res:
            c = _series_cand(s)
            c.title = de.get(s.get("tmdbId")) or c.title
            self._remember(chat_id, c)
            stats = s.get("statistics") or {}
            out.append(_compact({
                "tvdb": c.ext_id, "title": c.title, "year": c.year,
                "last_season": max(c.seasons) if c.seasons else None,
                "ended": s.get("status") == "ended",
                "lib_id": c.lib_id,
                "eps": f"{stats.get('episodeFileCount', 0)}/{stats.get('episodeCount', 0)}" if c.lib_id else None,
            }))
        return out

    async def _t_add_movie(self, chat_id: int, tmdb_id: int, quality_profile: str | None = None) -> Final:
        radarr: Radarr = self._need(self.radarr, "Radarr")
        if quality_profile:
            quality_profile = (await radarr.resolve_profile(quality_profile))["name"]
            await self._check_cap("movie", chat_id, quality_profile)
        cand = await self._candidate(chat_id, "movie", int(tmdb_id))
        if cand.lib_id:
            raise ArrError(f"'{cand.label}' ist schon in Radarr (lib_id {cand.lib_id}, Datei: {cand.has_file}).")
        return self._flow_final(await self._start_flow(chat_id, cand, profile=quality_profile))

    async def _t_add_series(
        self,
        chat_id: int,
        tvdb_id: int,
        monitor: str | None = None,
        seasons: list[int] | None = None,
        quality_profile: str | None = None,
    ) -> Final:
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        if quality_profile:
            quality_profile = (await sonarr.resolve_profile(quality_profile))["name"]
            await self._check_cap("series", chat_id, quality_profile)
        cand = await self._candidate(chat_id, "series", int(tvdb_id))
        if cand.lib_id:
            if seasons:
                return await self._search_existing(chat_id, "series", cand.lib_id, [int(x) for x in seasons])
            res = await self._start_extend(chat_id, cand)
            return Final(res.text) if isinstance(res, Step) else self._flow_final(res)
        flow = await self._start_flow(
            chat_id, cand, monitor=monitor, seasons=[int(s) for s in seasons] if seasons else None,
            profile=quality_profile,
        )
        return self._flow_final(flow)

    def _flow_final(self, flow: Flow) -> Final:
        assert flow.step is not None
        if flow.step.done:
            return Final(flow.step.text)
        self._outbox.setdefault(flow.chat_id, []).append(flow)
        return Final("")

    async def _t_offer_titles(self, chat_id: int, kind: str, ids: list[int]) -> Final:
        cands = [await self._candidate(chat_id, kind, int(i)) for i in ids[:6]]
        if not cands:
            raise ArrError("Keine IDs angegeben.")
        if len(cands) == 1:
            if cands[0].lib_id and kind == "series":
                res = await self._start_extend(chat_id, cands[0])
                return Final(res.text) if isinstance(res, Step) else self._flow_final(res)
            if cands[0].lib_id:
                raise ArrError(f"'{cands[0].label}' ist schon in der Bibliothek (lib_id {cands[0].lib_id}).")
            return self._flow_final(await self._start_flow(chat_id, cands[0]))
        self._offer(chat_id, cands)
        return Final("")

    def _offer(self, chat_id: int, cands: list[Candidate], text: str | None = None, bulk: bool = False,
               suggest: bool = False) -> Offer:
        offer = Offer(uuid.uuid4().hex[:10], chat_id, cands, bulk=bulk, suggest=suggest)
        if text:
            offer.text = text
        self.offers[offer.id] = offer
        while len(self.offers) > 200:
            self.offers.pop(next(iter(self.offers)))
        self._outbox.setdefault(chat_id, []).append(offer)
        return offer

    async def _t_search_existing(
        self, chat_id: int, kind: str, lib_id: int | None = None, seasons: list[int] | None = None,
        title: str | None = None,
    ) -> Final:
        """"Such nochmal": a title that is already in Radarr/Sonarr searches again."""
        if lib_id is None:
            if not title:
                raise ArrError("Welcher Titel? Nenn mir den Namen.")
            lib_id = await self._find_in_library(kind, title)
        return await self._search_again(chat_id, kind, int(lib_id), [int(s) for s in seasons] if seasons else None)

    @staticmethod
    def _norm(s: str) -> str:
        s = re.sub(r"[^a-z0-9äöüß]+", " ", (s or "").lower()).strip()
        return re.sub(r"^(the|der|die|das) ", "", s)

    async def _find_in_library(self, kind: str, title: str) -> int:
        """lib_id of a title in Radarr/Sonarr: exact (also alternative/original titles), else contained.
        Several hits: an error with the candidates so the AI can ask or pick."""
        svc = self._svc(kind)
        want = self._norm(title)
        items = await svc.library()

        def names(i: dict) -> list[str]:
            alt = [a.get("title", "") for a in i.get("alternateTitles") or []]
            return [self._norm(x) for x in [i.get("title", ""), i.get("originalTitle", ""), *alt] if x]

        hits = [i for i in items if want in names(i)]
        if not hits:
            hits = [i for i in items if any(want in n or (len(n) > 3 and n in want) for n in names(i))]
        if not hits:
            # German or other titles Sonarr/Radarr do not keep: ask their title search, it knows more names.
            try:
                found = await svc.lookup(title)
            except ArrError:
                found = []
            in_lib = {i["id"] for i in items}
            hits = [f for f in found[:5] if f.get("id") in in_lib]
        if not hits:
            what = "Film" if kind == "movie" else "Serie"
            raise ArrError(f"{title} ist als {what} nicht in der Bibliothek. Erst hinzufügen "
                           f"({'add_movie' if kind == 'movie' else 'add_series'}).")
        if len({h["id"] for h in hits}) > 1:
            listed = "; ".join(f"{h.get('title')} ({h.get('year')}) lib_id={h['id']}" for h in hits[:6])
            raise ArrError(f"Mehrere Treffer in der Bibliothek: {listed}. Mit lib_id nochmal aufrufen oder nachfragen.")
        return hits[0]["id"]

    async def _search_again(self, chat_id: int, kind: str, lib_id: int, seasons: list[int] | None) -> Final:
        counts: dict = {}
        if self.users is not None and not self._admin(chat_id):
            if kind == "movie":
                counts = {"filme": 1}
            else:
                missing, _ = await self._need(self.sonarr, "Sonarr").search_missing(lib_id, seasons, dry=True)
                counts = {"folgen": len(missing), "staffeln": len({e.get("seasonNumber") for e in missing})}
            await self._check_title_limit(chat_id, **counts)
            self.users.record_adds(chat_id, **counts)
        if kind == "movie":
            radarr: Radarr = self._need(self.radarr, "Radarr")
            movie = await radarr.get(lib_id)
            was_off = not movie.get("monitored", True)
            await radarr.monitor(movie)
            name = f"{movie['title']} ({movie.get('year')})" if movie.get("year") else movie["title"]
            if movie.get("hasFile"):
                note = " Er liegt schon vor – Radarr holt nur eine bessere Fassung, falls das Profil eine will."
            else:
                note = ""
            await radarr.search(movie["id"])
            self.store.track("radarr", movie["id"], name, chat_id)
            await self._added(chat_id, f"🔍 {name} (neue Suche)")
            off = " Er war nicht überwacht – jetzt schon, sonst lädt Radarr nichts." if was_off else ""
            return Final(f"🔍 Neue Suche für {name} gestartet.{off}{note} Ich melde mich, wenn der Download startet.")
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        series = await sonarr.get(lib_id)
        missing, was_off = await sonarr.search_missing(series["id"], seasons)
        where = f" in Staffel {', '.join(map(str, seasons))}" if seasons else ""
        if not missing:
            if seasons:
                self.store.track("sonarr", series["id"], series["title"], chat_id)
                return Final(f"👁 {series['title']}{where.replace(' in ', ': ')} wird überwacht. Bisher fehlt keine "
                             "erschienene Folge; neue lädt Sonarr, sobald sie da sind.")
            return Final(f"👍 Bei {series['title']} fehlt keine bereits erschienene Folge – nichts zu suchen.")
        self.store.track("sonarr", series["id"], series["title"], chat_id)
        await self._added(chat_id, f"🔍 {series['title']}{where} ({len(missing)} Folgen neu gesucht)")
        eps = manage._eps_text(missing)
        off = " Die Serie war nicht überwacht – jetzt schon, sonst lädt Sonarr nichts." if was_off else ""
        n = len(missing)
        return Final(f"🔍 Neue Suche für {series['title']}: {n} fehlende Folge{'n' if n != 1 else ''}"
                     f"{f' ({eps})' if eps else ''}.{off} Ich melde mich, wenn Downloads starten.")

    async def _search_existing(self, chat_id: int, kind: str, lib_id: int, seasons: list[int] | None) -> Final:
        if kind == "movie":
            radarr: Radarr = self._need(self.radarr, "Radarr")
            movie = await radarr.get(lib_id)
            await radarr.search(movie["id"])
            self.store.track("radarr", movie["id"], f"{movie['title']} ({movie.get('year')})", chat_id)
            self.store.record_request(chat_id, "movie", f"{movie['title']} ({movie.get('year')})", movie.get("tmdbId"))
            self._learn(chat_id, "requested", "movie", movie)
            await self._added(chat_id, f"🔍 {movie['title']} (neue Suche)")
            return Final(f"🔍 Suche für {movie['title']} gestartet. Ich melde mich, wenn der Download startet.")
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        series = await sonarr.get(lib_id)
        self.store.record_request(chat_id, "series", series["title"], series.get("tmdbId"), series.get("tvdbId"))
        self._learn(chat_id, "requested", "series", series)
        # Same as "such nochmal": monitors series/seasons/episodes (an unmonitored series is skipped by
        # Sonarr without a word) and searches only what is missing, whole seasons as season packs.
        return await self._search_again(chat_id, "series", series["id"], seasons)

    # ---- delete / pause / resume ---------------------------------------------------------
    def owns(self, uid: int, kind: str, item: dict) -> bool:
        """Did uid request this title (via the bot)?"""
        for r in self.store.state.requests:
            if r["chat_id"] != uid or r["kind"] != kind:
                continue
            if kind == "movie" and r.get("tmdb") and r["tmdb"] == item.get("tmdbId"):
                return True
            if kind == "series" and ((r.get("tvdb") and r["tvdb"] == item.get("tvdbId"))
                                     or (r.get("tmdb") and r["tmdb"] == item.get("tmdbId"))):
                return True
        return False

    async def _managed(self, chat_id: int, kind: str, lib_id: int, delete: bool = False,
                       perm: str | None = None) -> tuple[dict, str, str]:
        """(item, key, title) if chat_id may change this title. perm: delete | pause | repair.
        Admin: always. Users: only with that right from the admin; delete_own / pause only their own titles."""
        svc = self._svc(kind)
        item = await svc.get(int(lib_id))
        service = "radarr" if kind == "movie" else "sonarr"
        key = f"{service}:{item['id']}"
        title = f"{item.get('title')} ({item.get('year')})" if kind == "movie" and item.get("year") else item.get("title", "?")
        if self._admin(chat_id):
            return item, key, title
        perm = perm or ("delete" if delete else "repair")
        has = (lambda p: self.users is not None and self.users.has_perm(chat_id, p))
        if perm == "delete" and has("delete"):
            return item, key, title
        if perm == "delete" and has("delete_own"):
            if self.owns(chat_id, kind, item):
                return item, key, title
            raise ArrError(f"{title} hast nicht du angefragt – löschen darfst du nur deine eigenen Titel.")
        if perm == "pause" and has("pause"):
            if self.owns(chat_id, kind, item):
                return item, key, title
            raise ArrError(f"{title} hast nicht du angefragt – pausieren darfst du nur deine eigenen Titel.")
        if perm == "repair" and has("repair"):
            return item, key, title
        raise ArrError(f"{title} ändern oder löschen kann nur der Admin.")

    async def start_remove(self, chat_id: int, kind: str, lib_id: int) -> Flow:
        item, key, title = await self._managed(chat_id, kind, lib_id, delete=True)
        tool = "Radarr" if kind == "movie" else "Sonarr"
        if kind == "movie":
            has_files = bool(item.get("hasFile"))
            size = (item.get("sizeOnDisk") or 0) / 2**30
        else:
            st = item.get("statistics") or {}
            has_files = bool(st.get("episodeFileCount"))
            size = (st.get("sizeOnDisk") or 0) / 2**30
        cand = Candidate(kind, item.get("tmdbId") if kind == "movie" else item.get("tvdbId"), title,
                         item.get("year"), lib_id=item["id"])
        flow = Flow(uuid.uuid4().hex[:10], chat_id, cand, stage="remove")
        self.flows[flow.id] = flow
        if has_files:
            gb = f" ({size:.1f} GB)".replace(".", ",") if size else ""
            text = f"🗑 {title} löschen?\nEs sind Dateien vorhanden{gb}."
            buttons = [("🗑 Löschen inkl. Dateien", "files"), (f"📋 Nur aus {tool} entfernen", "keep")]
        else:
            text = f"🗑 {title} aus {tool} löschen? Laufende Downloads werden abgebrochen."
            buttons = [("🗑 Ja, löschen", "keep")]
        flow.step = Step(text, buttons + [("Abbrechen", "no")])
        return flow

    async def _do_remove(self, flow: Flow, value: str) -> Step:
        c = flow.cand
        if value not in ("files", "keep"):
            return Step(f"👍 {c.title} bleibt.", done=True)
        try:
            item = await self._svc(c.kind).get(c.lib_id)
        except ArrError:
            item = {}
        try:
            await self._svc(c.kind).delete(c.lib_id, delete_files=value == "files")
        except ArrError as e:
            return Step(f"❌ {c.title} konnte nicht gelöscht werden: {e}", done=True)
        self.store.forget(f"{'radarr' if c.kind == 'movie' else 'sonarr'}:{c.lib_id}")
        self._seen.get(flow.chat_id, {}).pop((c.kind, c.ext_id), None)
        if item:
            self._learn(flow.chat_id, "deleted", c.kind, item, c.title)
        what = " inkl. Dateien" if value == "files" else " (Dateien bleiben)"
        await self._added(flow.chat_id, f"🗑 {c.title} gelöscht{what}")
        return Step(f"🗑 {c.title} ist gelöscht{what}.", done=True)

    async def _t_remove_title(self, chat_id: int, kind: str, lib_id: int) -> Final:
        flow = await self.start_remove(chat_id, kind, int(lib_id))
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def pause(self, chat_id: int, kind: str, lib_id: int, days: int | None) -> str:
        item, key, title = await self._managed(chat_id, kind, lib_id, perm="pause")
        await self._svc(kind).set_monitored(item["id"], False)
        until = (utcnow() + timedelta(days=days)) if days else None
        entry = self.store.state.paused.get(key) or {"title": title, "chat_ids": []}
        if chat_id not in entry["chat_ids"]:
            entry["chat_ids"].append(chat_id)
        entry["until"] = until.isoformat() if until else None
        self.store.state.paused[key] = entry
        self.store.save()
        if until:
            when = "morgen" if days == 1 else f"am {until.astimezone(self.tz):%d.%m.}"
            return f"⏸ Suche nach {title} pausiert. Ich suche {when} wieder und sage dir dann Bescheid."
        return f"⏸ Suche nach {title} pausiert. Sag „weiter mit {item.get('title')}“, wenn ich wieder suchen soll."

    async def resume(self, chat_id: int, kind: str, lib_id: int) -> str:
        item, key, title = await self._managed(chat_id, kind, lib_id, perm="pause")
        await resume_search(self._svc(kind), item["id"])
        entry = self.store.state.paused.pop(key, None)
        for chat in (entry or {}).get("chat_ids") or [chat_id]:
            self.store.track("radarr" if kind == "movie" else "sonarr", item["id"], title, chat)
        return f"▶️ Suche nach {title} läuft wieder. Ich melde mich, wenn der Download startet."

    async def _t_pause_title(self, chat_id: int, kind: str, lib_id: int, days: int | None = None) -> Final:
        return Final(await self.pause(chat_id, kind, int(lib_id), int(days) if days else None))

    async def _t_resume_title(self, chat_id: int, kind: str, lib_id: int) -> Final:
        return Final(await self.resume(chat_id, kind, int(lib_id)))

    async def _t_list_quality_profiles(self, chat_id: int, kind: str) -> list[str]:
        return [p["name"] for p in await self.allowed_profiles(kind, chat_id)]

    async def _t_download_status(self, chat_id: int, finished: bool = False, title: str | None = None) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "downloads", bool(finished), title or None))
        return Final("")

    # ---- series housekeeping (German episodes, deleting episodes) ----------------------------
    async def _series_job(self, chat_id: int, tvdb_id: int, stage: str, text: str,
                          buttons: list[tuple[str, str]], plan: Any) -> Final:
        if not buttons:
            return Final(text)
        cand = await self._candidate(chat_id, "series", int(tvdb_id))
        flow = Flow(uuid.uuid4().hex[:10], chat_id, cand, stage=stage, data=plan)
        flow.step = Step(text, buttons)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _library_series(self, chat_id: int, tvdb_id: int, delete: bool) -> int:
        cand = await self._candidate(chat_id, "series", int(tvdb_id))
        if not cand.lib_id:
            raise ArrError(f"{cand.title} ist nicht in der Bibliothek. Erst hinzufügen (add_series).")
        await self._managed(chat_id, "series", cand.lib_id, perm="repair")
        return cand.lib_id

    async def _t_german_fix(self, chat_id: int, tvdb_id: int | None = None, season: int | None = None,
                            tmdb_id: int | None = None) -> Final:
        if tmdb_id and not tvdb_id:
            return await self._movie_fix(chat_id, int(tmdb_id))
        if not tvdb_id:
            raise ArrError("tvdb_id (Serie) oder tmdb_id (Film) angeben.")
        lib_id = await self._library_series(chat_id, tvdb_id, delete=False)
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        plan = await manage.plan_german(sonarr, lib_id, int(season) if season else None, date.today())
        text, buttons = manage.render_plan(plan)
        return await self._series_job(chat_id, tvdb_id, "gfix", text, buttons, plan)

    @staticmethod
    async def _rescan(sonarr: Any, series_id: int, wait: float = 60.0) -> None:
        """Let Sonarr read the series folder again and wait (max. wait seconds) until it is done."""
        cmd = await sonarr.command("RescanSeries", seriesId=series_id)
        loop = asyncio.get_running_loop()
        end = loop.time() + wait
        while cmd.get("id") and loop.time() < end:
            state = await sonarr._req("GET", f"/command/{cmd['id']}") or {}
            if state.get("status") in ("completed", "failed", "aborted", "cancelled"):
                return
            await asyncio.sleep(2)

    async def _t_clean_episodes(self, chat_id: int, tvdb_id: int, which: str, season: int | None = None) -> Final:
        if which == "season" and not season:
            raise ArrError("Welche Staffel? season angeben.")
        lib_id = await self._library_series(chat_id, tvdb_id, delete=True)
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        plan = await manage.plan_clean(sonarr, lib_id, which, int(season) if season else None)
        text, buttons = manage.render_clean(plan)
        return await self._series_job(chat_id, tvdb_id, "clean", text, buttons, plan)

    async def _movie_fix(self, chat_id: int, tmdb_id: int) -> Final:
        cand = await self._candidate(chat_id, "movie", tmdb_id)
        if not cand.lib_id:
            raise ArrError(f"{cand.title} ist nicht in Radarr.")
        await self._managed(chat_id, "movie", cand.lib_id, perm="repair")
        radarr: Radarr = self._need(self.radarr, "Radarr")
        fix = await manage.plan_movie(radarr, cand.lib_id)
        text, buttons = manage.render_movie(fix)
        if not buttons:
            return Final(text)
        flow = Flow(uuid.uuid4().hex[:10], chat_id, cand, stage="mfix", data=fix)
        flow.step = Step(text, buttons)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _run_series_job(self, flow: Flow, value: str) -> Step:
        plan, title = flow.data, flow.cand.title
        if value == "ok" and flow.stage in ("gfix", "mfix"):
            kind, arr_id = ("m", plan.movie_id) if flow.stage == "mfix" else ("s", plan.series_id)
            german.confirmed.add(kind, arr_id)
            return Step(f"👍 Gemerkt: {title} ist Deutsch, nur die Tonspur ist falsch markiert. "
                        "Ich melde das nicht mehr.", done=True)
        if value == "notify" and flow.stage == "gfix":
            res = await self._t_notify_german(flow.chat_id, "series", flow.cand.ext_id)
            return Step(res.text or f"🔔 Ich sage Bescheid, sobald es {title} auf Deutsch gibt.", done=True)
        if value == "rescan" and flow.stage == "gfix":
            sonarr_ = self._need(self.sonarr, "Sonarr")
            try:
                await self._rescan(sonarr_, plan.series_id)
                flow.data = plan = await manage.plan_german(sonarr_, plan.series_id, plan.season, date.today())
            except ArrError as e:
                return Step(f"❌ Neu einlesen hat nicht geklappt: {e}", done=True)
            text, buttons = manage.render_plan(plan)
            text = "🔄 Sonarr hat den Ordner neu eingelesen.\n" + text
            return Step(text, [b for b in buttons if b[1] != "rescan"]) if len(buttons) > 1 else Step(text, done=True)
        if value == "more" and flow.stage == "gfix":
            try:
                await manage.search_more(self._need(self.sonarr, "Sonarr"), plan)
            except ArrError as e:
                return Step(f"❌ Suche hat nicht geklappt: {e}", done=True)
            text, buttons = manage.render_plan(plan)
            return Step(text, buttons) if buttons else Step(text, done=True)
        if value not in ("all", "missing", "research", "drop"):
            return Step(f"👍 Okay, bei {title} bleibt alles, wie es ist.", done=True)
        if flow.stage == "mfix":
            radarr: Radarr = self._need(self.radarr, "Radarr")
            try:
                text = await manage.run_movie(radarr, flow.data)
            except ArrError as e:
                return Step(f"❌ Hat nicht geklappt: {e}", done=True)
            self.store.track("radarr", flow.data.movie_id, title, flow.chat_id)
            await self._added(flow.chat_id, f"🇩🇪 {title}: deutsche Fassung wird geladen")
            return Step(text, done=True)
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        try:
            if flow.stage == "gfix":
                text = await manage.run_fix(sonarr, plan, only_missing=value == "missing")
                await self._added(flow.chat_id, f"🇩🇪 {title}: deutsche Folgen geladen")
            else:
                text = await manage.run_clean(sonarr, plan, research=value == "research")
                await self._added(flow.chat_id, f"🗑 {title}: {len(plan.episodes)} Folgen gelöscht")
        except ArrError as e:
            return Step(f"❌ Hat nicht geklappt: {e}", done=True)
        self.store.track("sonarr", plan.series_id, title, flow.chat_id)  # report the new downloads
        return Step(text, done=True)

    # ---- admin ------------------------------------------------------------------------
    async def _t_admin_quality_profile(self, chat_id: int, name: str, service: str | None = None,
                                       min_resolution: int | None = None, max_resolution: int | None = None,
                                       required_languages: list[str] | None = None,
                                       preferred_languages: list[str] | None = None, only: bool = False) -> Final:
        w = qprofiles.make_wish(name, service, min_resolution, max_resolution, required_languages,
                                preferred_languages, only)
        w.services = [s for s in w.services if (self.radarr if s == "radarr" else self.sonarr)]
        if not w.services:
            raise ArrError("Dafür ist weder Radarr noch Sonarr eingerichtet.")
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, w.name, None), stage="qprof", data=w)
        flow.step = Step(w.summary() + "\n\nSo anlegen?", [("✅ Anlegen", "yes"), ("Abbrechen", "no")])
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _create_profiles(self, flow: Flow, value: str) -> Step:
        w = flow.data
        if value != "yes":
            return Step("👍 Okay, kein neues Profil.", done=True)
        lines = []
        for s in w.services:
            svc = self.radarr if s == "radarr" else self.sonarr
            try:
                lines.append(await qprofiles.create(svc, w))
            except ArrError as e:
                lines.append(f"❌ {svc.kind}: {e}")
        lines.append(f"Nutzen: „Lad Dune in {w.name}“ oder als Standard über RADARR_QUALITY_PROFILE / "
                     "SONARR_QUALITY_PROFILE.")
        return Step("\n".join(lines), done=True)

    async def _t_admin_media(self, chat_id: int, section: str = "overview", days: int | None = None) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "media", arg=(section, days)))
        return Final("")

    async def _t_suggest_together(self, chat_id: int, with_user: str, kind: str | None = None) -> Final:
        if self.profiles is None or self.users is None:
            raise ArrError("Gemeinsame Vorschläge brauchen Nutzerprofile.")
        other = self.users.find(with_user)
        if other is None or not self.users.is_allowed(other):
            raise ArrError(f"„{with_user}“ kenne ich nicht – der Name muss so heißen wie in der Nutzerliste.")
        if other == chat_id:
            raise ArrError("Das bist du selbst. 🙂 Mit wem willst du schauen?")
        a, b = self.profiles.profile(chat_id), self.profiles.profile(other)
        kinds = [kind] if kind else ["movie", "series"]

        here: list[together.Pick] = []
        acc_a = self.plex_store.get(chat_id) if self.plex_store else None
        acc_b = self.plex_store.get(other) if self.plex_store else None
        if self.plex and (acc_a or acc_b):
            try:
                main, extra = (acc_a, acc_b) if acc_a else (acc_b, None)
                items_main = await self.plex.library(main)
                items_extra = await self.plex.library(extra) if extra else None
                here = [p for p in together.from_library(items_main, items_extra, a, b) if p.kind in kinds][:6]
            except plex.PlexError as e:
                log.info("Plex für gemeinsame Vorschläge: %s", e)

        cands: list[Candidate] = []
        if self.tmdb:
            owned = await self._library_ids([k for k in kinds if (self.radarr if k == "movie" else self.sonarr)])
            seeds = [s for s in self.profiles.recent(chat_id, 5) + self.profiles.recent(other, 5)
                     if s.get("tmdb") and s.get("kind") in kinds]
            pool: dict[tuple[str, int], tuple[float, Any]] = {}
            results = await asyncio.gather(*(self.tmdb.recommendations(s["kind"], s["tmdb"]) for s in seeds),
                                           return_exceptions=True)
            for s, res in zip(seeds, results):
                if isinstance(res, BaseException):
                    continue
                for rank, r in enumerate(res[:20]):
                    key = (s["kind"], r.tmdb_id)
                    if key in owned["tmdb"] or together.excluded(a, b, *key):
                        continue
                    score = together.fit(a, b, r.genres, r.rating)
                    if score is None:
                        continue
                    old = pool.get(key, (0.0, r))[0]
                    pool[key] = (old + score + (20 - rank) / 40, r)  # recommended for both: adds up
            best = sorted(pool.items(), key=lambda kv: -kv[1][0])[:5]
            for (k, tid), (_, r) in best:
                if k == "movie":
                    cands.append(Candidate("movie", r.tmdb_id, r.title, r.year, rating=r.rating, tmdb_id=r.tmdb_id,
                                           genres=r.genres))
                else:
                    tvdb = await self.tmdb.tvdb_from_tv(r.tmdb_id)
                    if tvdb:
                        cands.append(Candidate("series", tvdb, r.title, r.year, rating=r.rating, tmdb_id=r.tmdb_id,
                                               genres=r.genres))
        text = together.render(self.users.name(chat_id), self.users.name(other), here, len(cands))
        if cands:
            self._offer(chat_id, cands, "➕ Neu für euch:", suggest=True)
        return Final(text)

    async def _t_wishlist(self, chat_id: int, action: str, kind: str | None = None, id: int | None = None,  # noqa: A002
                          wish_id: int | None = None) -> Final:
        if self.wishes is None:
            raise ArrError("Die Wunschliste ist nicht eingerichtet.")
        if action == "remove":
            if wish_id is None or not self.wishes.remove(int(wish_id), chat_id):
                raise ArrError(f"Wunsch {wish_id} gibt es nicht.")
            return Final(f"🗑 Wunsch {wish_id} gestrichen.")
        if action != "add":
            return Final(wishlist.render(self.wishes.wishes(chat_id)))
        if kind not in ("movie", "series") or not id:
            raise ArrError("Erst den Titel suchen, dann mit seiner ID vormerken.")
        svc = self._need(self.radarr if kind == "movie" else self.sonarr, "Radarr" if kind == "movie" else "Sonarr")
        item = await (svc.lookup_tmdb(int(id)) if kind == "movie" else svc.lookup_tvdb(int(id)))
        title = item.get("title") or "?"
        name = f"{title} ({item['year']})" if item.get("year") else title
        if item.get("id"):
            return Final(f"👍 {name} ist schon da.")
        when, what = wishlist.release_date(kind, item)
        if when and when <= date.today():
            cand = await self._candidate(chat_id, kind, int(id))
            self._offer(chat_id, [cand], f"📅 {name} ist schon erschienen ({what} {when:%d.%m.%Y}). Gleich laden?")
            return Final("")
        if when:  # there is a date already: add it now, it loads on its own once released
            flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate(kind, int(id), title, item.get("year")), monitor="all")
            try:
                await self._add(flow)
            except Held:
                return Final(f"📅 {name} kommt am {when:%d.%m.%Y} ({what}). Der Wunsch wartet auf die Freigabe.")
            return Final(f"📅 {name} hat schon einen Termin ({what} {when:%d.%m.%Y}). Ich habe es angelegt – "
                         "es lädt von selbst, sobald es verfügbar ist.")
        if not self.wishes.add(chat_id, kind, int(id), name):
            return Final(f"📌 {name} steht schon auf deiner Wunschliste.")
        return Final(f"📌 {name} ist gemerkt. Noch gibt es keinen Termin – sobald es einen gibt, lege ich es an "
                     "und sage dir Bescheid.")

    async def _t_watch_site(self, chat_id: int, action: str, url: str | None = None, kind: str = "all",
                            site_id: int | None = None) -> Final:
        if self.sites is None:
            raise ArrError("Seiten-Beobachtung ist nicht eingerichtet.")
        if action == "add":
            if not url:
                raise ArrError("Welche Seite soll ich beobachten? Schick mir den Link.")
            url = url.strip() if "://" in url else "https://" + url.strip()
            mine = self.sites.sites(chat_id)
            if any(s.url == url for s in mine):
                return Final("👀 Die Seite beobachte ich schon für dich.")
            if len(mine) >= sitewatch.MAX_SITES_PER_USER and not self.is_admin(chat_id):
                raise ArrError(f"Du beobachtest schon {len(mine)} Seiten – lösch erst eine („Beobachtete Seiten“).")
            try:
                final_url, page = await urlinfo.fetch(url)
            except urlinfo.UrlError as e:
                raise ArrError(f"Seite nicht lesbar: {e}") from e
            lines = sitewatch.page_lines(page)
            if len(lines) < 5:
                raise ArrError("Auf der Seite steht kaum Text (lädt sie erst per JavaScript?). Die kann ich nicht beobachten.")
            site = self.sites.add(chat_id, final_url, kind or "all", lines)
            what = {"movie": "Filme", "series": "Serien"}.get(site.kind, "Filme und Serien")
            return Final(f"👀 Ich beobachte {site.host} auf neue {what} und schaue alle {self.site_hours} Std. nach. "
                         "Nur was neu dazukommt, zeige ich dir – mit Buttons zum Hinzufügen. "
                         "„Nichts davon“ merke ich mir für bessere Vorschläge.")
        if action == "remove":
            mine = None if self.is_admin(chat_id) else chat_id
            if site_id is None or not self.sites.remove(int(site_id), mine):
                raise ArrError(f"Seite {site_id} gibt es nicht.")
            return Final(f"🗑 Seite {site_id} wird nicht mehr beobachtet.")
        return Final(sitewatch.render_sites(self.sites.sites(chat_id)))

    async def check_site(self, site: sitewatch.Site, provider: Any) -> dict:
        """One look at a watched page. New titles go to the user as buttons. Returns the AI usage."""
        _, page = await urlinfo.fetch(site.url)
        lines = sitewatch.page_lines(page)
        if sitewatch.digest(lines) == site.digest:
            self.sites.touch(site)  # type: ignore[union-attr]
            return {}
        fresh = sitewatch.new_lines(site, lines)
        if not fresh:
            self.sites.update(site, lines, 0)  # type: ignore[union-attr]
            return {}
        items, usage = await sitewatch.extract(provider, fresh)
        found = await sitewatch.resolve(items, site.kind, self.radarr, self.sonarr, profiles.genre_de)
        prof = self.profiles.profile(site.user_id) if self.profiles else None
        avoid = {g.lower() for g in (prof.prefs.avoid if prof else [])}
        cands = []
        for f in found:
            if f["have"] or {g.lower() for g in f["genres"]} & avoid:
                continue
            if prof and f["tmdb_id"] and ((f["kind"], f["tmdb_id"]) in prof.rejected or (f["kind"], f["tmdb_id"]) in prof.seen):
                continue
            cands.append(Candidate(f["kind"], f["ext_id"], f["title"], f["year"], tmdb_id=f["tmdb_id"],
                                   genres=f["genres"], rating=0.0))
        if prof:
            cands.sort(key=lambda c: -prof.genre_bonus(c.genres))
        self.sites.update(site, lines, len(cands))  # type: ignore[union-attr]
        if cands:
            self._offer(site.user_id, cands[:sitewatch.MAX_OFFER], f"🌐 Neu auf {site.host}:", bulk=len(cands) > 1,
                        suggest=True)
        return usage

    def plex_admin(self) -> plex.Account | None:
        """The Plex account of an admin (collections need the server owner)."""
        if not self.plex_store or self.users is None:
            return None
        return next((a for a in self.plex_store.all() if self.is_admin(a.user_id)), None)

    async def _t_admin_plex_collection(self, chat_id: int, action: str, name: str | None = None,
                                       titles: list[dict] | None = None) -> Final:
        acc = self.plex_admin()
        if not (self.plex and acc):
            raise ArrError("Dafür muss ein Admin sein Plex verbinden (/plex) – Sammlungen legt der Server-Besitzer an.")
        if action in ("auto_on", "auto_off"):
            self.plex_store.set_meta("auto_collections", "1" if action == "auto_on" else None)  # type: ignore[union-attr]
            if action == "auto_off":
                return Final("📚 Keine automatischen Wunsch-Sammlungen mehr (die vorhandenen bleiben in Plex).")
            done = await self.sync_wish_collections()
            return Final("📚 Ab jetzt hat jeder eine Sammlung „Wünsche von …“ in Plex, täglich aktualisiert."
                         + (f"\n{done}" if done else ""))
        if not name or not titles:
            raise ArrError("Für eine Sammlung brauche ich einen Namen und die Titel.")
        try:
            library = await self.plex.library(acc)
            items, missing = collections_plex.match(library, titles)
            if not items:
                return Final(f"📚 Von den {len(titles)} Titeln für „{name}“ ist keiner in Plex.")
            res = await collections_plex.Collections(self.plex).put(acc, name.strip()[:60], items, missing)
        except plex.PlexError as e:
            raise ArrError(str(e)) from e
        return Final(res.text())

    async def sync_wish_collections(self) -> str:
        """'Wünsche von <Name>' for every user with requests that are in Plex (daily when switched on)."""
        acc = self.plex_admin()
        if not (self.plex and acc and self.users is not None):
            return ""
        library = await self.plex.library(acc)
        coll = collections_plex.Collections(self.plex)
        lines = []
        for uid in sorted({r["chat_id"] for r in self.store.state.requests}):
            if not self.users.is_allowed(uid):
                continue
            items = collections_plex.wish_items(self.store.state.requests, uid, library)
            if not items:
                continue
            res = await coll.put(acc, f"Wünsche von {self.users.name(uid)}", items)
            if res.added:
                lines.append(f"„Wünsche von {self.users.name(uid)}“: +{len(res.added)}")
        return "\n".join(lines)

    async def _t_admin_approval(self, chat_id: int, action: str, user: str | None = None, kind: str = "all",
                                genres: list[str] | None = None, min_gb: float | None = None,
                                rule_id: int | None = None) -> Final:
        if self.approvals is None:
            raise ArrError("Freigaben sind nicht eingerichtet.")
        if action == "add":
            who = self.users.find(user) if user and user.lower() not in ("alle", "allen", "jeder", "all") else None
            if user and who is None and user.lower() not in ("alle", "allen", "jeder", "all"):
                raise ArrError(f"Nutzer {user} nicht gefunden.")
            rule = self.approvals.add(who, kind or "all", genres, min_gb)
            return Final(f"✋ Regel {rule.id}: {rule.text(self.users.name)} – erst nach deiner Freigabe.")
        if action == "remove":
            if rule_id is None or not self.approvals.remove(int(rule_id)):
                raise ArrError(f"Regel {rule_id} gibt es nicht.")
            return Final(f"🗑 Freigabe-Regel {rule_id} gelöscht.")
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "approvals"))
        return Final("")

    async def _t_admin_cleanup(self, chat_id: int, action: str, what: str | None = None, kind: str = "all",
                               days: int | None = None, low_space_gb: int | None = None, auto: bool = False,
                               rule_id: int | None = None) -> Final:
        if self.cleanup is None:
            raise ArrError("Aufräumregeln sind nicht eingerichtet.")
        if action == "add":
            if not what or not days:
                raise ArrError("Für eine Regel brauche ich: gesehen oder ungesehen, und nach wie vielen Tagen.")
            rule = self.cleanup.add(what, kind or "all", int(days), low_space_gb, bool(auto))
            return Final(f"🧹 Regel {rule.id} angelegt: {rule.text}.\nIch prüfe täglich um 11 Uhr – "
                         "oder sofort: „Aufräumregeln jetzt prüfen“.")
        if action == "remove":
            if rule_id is None or not self.cleanup.remove(int(rule_id)):
                raise ArrError(f"Regel {rule_id} gibt es nicht.")
            return Final(f"🗑 Regel {rule_id} gelöscht.")
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "cleanup", arg=action == "run"))
        return Final("")

    # ---- duplicates (admin) ------------------------------------------------------------------
    def _dupe_prefs(self) -> dict:
        return dupes.load_prefs(self.cleanup.setting("dupes") if self.cleanup else None)

    async def _t_admin_duplicates(self, chat_id: int, action: str = "scan", kind: str = "all",
                                  similarity: float | None = None, languages: list[str] | None = None,
                                  resolutions: str | None = None, language_first: bool | None = None) -> Final:
        prefs = self._dupe_prefs()
        changed = False
        if similarity is not None:
            prefs["similarity"] = max(50.0, min(100.0, float(similarity)))
            changed = True
        if languages:
            prefs["languages"] = [x for x in (dupes.norm_language(v) for v in languages) if x]
            changed = True
        if resolutions:
            prefs["resolutions"] = dupes.parse_resolutions(resolutions)
            changed = True
        if language_first is not None:
            prefs["language_first"] = bool(language_first)
            changed = True
        if changed and self.cleanup is not None:
            self.cleanup.set_setting("dupes", json.dumps(prefs))
        if action == "settings":
            return Final("🧬 " + dupes.prefs_text(prefs))
        acc = self._plex_of(chat_id)
        if not (self.plex and acc):
            raise ArrError("Der Duplikat-Finder braucht Plex – verbinde es mit /plex (als Server-Besitzer).")
        metas: list[dict] = []
        try:
            for s in await self.plex.sections(acc):
                if s.get("type") == "movie" and kind in ("all", "movie"):
                    metas += await self.plex.section_items(acc, s["key"])
                elif s.get("type") == "show" and kind in ("all", "series"):
                    metas += await self.plex.section_items(acc, s["key"], episodes=True)
        except plex.PlexError as e:
            raise ArrError(str(e)) from e
        # Audio languages only come with the details: fetch them for the titles that matter.
        wanted = [m for g in dupes.groups(metas) for m in g]
        rest, wanted = wanted[400:], wanted[:400]  # the rest without audio details, but not left out
        sem = asyncio.Semaphore(4)

        async def detail(m: dict) -> dict:
            async with sem:
                try:
                    full = (await self.plex.metadata(acc, str(m.get("ratingKey")))) or {}
                except plex.PlexError:
                    return m
            return {**m, "Media": full.get("Media") or m.get("Media")}

        job = dupes.find(list(await asyncio.gather(*(detail(m) for m in wanted))) + rest, prefs)
        job.no_details = len(rest)
        text, buttons = dupes.full_list(job) if action == "list" and job.items else dupes.preview(job, prefs)
        if not buttons:
            return Final(text)
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Duplikate", None), stage="dup", data=job)
        flow.step = Step(text, buttons)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _delete_dupes(self, chat_id: int, job: Any, items: list[Any]) -> None:
        acc = self._plex_of(chat_id)
        rescan: set[tuple] = set()
        for d in items:
            try:
                await self.plex.delete_media(acc, d.drop.rating_key, d.drop.media_id)
            except plex.PlexError as e:
                job.failed.append(f"{d.name}: {e}")
                continue
            job.deleted.append(d)
            if d.ext:
                rescan.add(d.ext)
        for kind, ext in rescan:  # Radarr/Sonarr: notice which file is left
            try:
                if kind == "movie" and self.radarr:
                    hit = next((m for m in await self.radarr.library() if m.get("tmdbId") == ext), None)
                    if hit:
                        await self.radarr.command("RescanMovie", movieIds=[hit["id"]])
                elif kind == "series" and self.sonarr:
                    hit = next((s for s in await self.sonarr.library()
                                if self._norm(s.get("title") or "") == self._norm(str(ext))), None)
                    if hit:
                        await self.sonarr.command("RescanSeries", seriesId=hit["id"])
            except ArrError as e:
                log.info("Neu einlesen nach Duplikat: %s", e)

    async def _dup_step(self, flow: Flow, value: str) -> Step:
        job = flow.data
        if value == "no":
            return Step(dupes.summary(job, cancelled=True), done=True)
        if value == "all":
            await self._delete_dupes(flow.chat_id, job, [d for d in job.items if d.sure])
            return Step(dupes.summary(job), done=True)
        if value in ("del", "skip") and job.pos < len(job.items):
            if value == "del":
                await self._delete_dupes(flow.chat_id, job, [job.items[job.pos]])
            else:
                job.skipped += 1
            job.pos += 1
        elif value == "one":
            job.pos = 0
        elif value == "list" or value.startswith("pg:"):
            job.page += {"pg:next": 1, "pg:prev": -1}.get(value, 0) if value != "list" else -job.page
            text, buttons = dupes.full_list(job)
            return Step(text, buttons)
        if value == "stop" or job.pos >= len(job.items):
            return Step(dupes.summary(job), done=True)
        text, buttons = dupes.one(job)
        return Step(text, buttons)

    # ---- media manager (admin): delete on command, storage, keep ------------------------------------
    def _plex_of(self, chat_id: int) -> plex.Account | None:
        if not self.plex_store:
            return None
        return self.plex_store.get(chat_id) or self.plex_admin()

    async def _media_report(self) -> Any:
        from . import media

        accounts = [a for a in (self.plex_store.all() if self.plex_store else [])
                    if self.users is None or self.users.is_allowed(a.user_id)]
        return await media.collect(self.radarr, self.sonarr, self.plex if accounts else None, accounts,
                                   german_check=False)

    def _recent_requests(self) -> dict[tuple[str, int], int]:
        cutoff = utcnow() - timedelta(days=mediamgr.PROTECT_DAYS)
        out: dict[tuple[str, int], int] = {}
        for r in self.store.state.requests:
            try:
                at = datetime.fromisoformat(r.get("at") or "")
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            ext = r.get("tmdb") if r["kind"] == "movie" else r.get("tvdb")
            if at >= cutoff and ext:
                out[(r["kind"], ext)] = r["chat_id"]
        return out

    async def _t_admin_delete_media(self, chat_id: int, kind: str = "all", watched: bool | None = None,
                                    older_days: int | None = None, seen_days: int | None = None,
                                    min_gb: float | None = None, max_rating: float | None = None,
                                    genres: list[str] | None = None, ended: bool | None = None,
                                    scope: str | None = None, largest: int | None = None) -> Final:
        q = mediamgr.Query(kind or "all", watched, older_days, seen_days, min_gb, max_rating, list(genres or []),
                           ended, scope or "", largest)
        if q.kind == "movie" and not self.radarr or q.kind == "series" and not self.sonarr:
            raise ArrError("Dafür fehlt Radarr bzw. Sonarr.")
        acc = self._plex_of(chat_id)
        if (watched is not None or seen_days) and not (self.plex and acc):
            raise ArrError("Für „gesehen“ brauche ich deinen Plex-Verlauf – verbinde Plex mit /plex.")
        if not any((watched is not None, older_days, seen_days, min_gb, max_rating, genres, largest, ended)):
            raise ArrError("Das wäre ALLES. Was genau soll weg (gesehen, ungesehen, älter als …, die größten …)? "
                           "Bitte nachfragen.")
        report = await self._media_report()
        kept = self.cleanup.kept() if self.cleanup else set()
        name_of = (lambda u: self.users.name(u)) if self.users is not None else (lambda u: str(u))
        hits, protected = mediamgr.select(report, q, acc.user_id if acc else None, kept, self._recent_requests(),
                                          name_of)
        job = mediamgr.Job(q, protected=protected)
        series = [t for t in hits if t.kind == "series"]
        job.items = [mediamgr.whole(t) for t in hits if t.kind == "movie"]
        if series and not q.scope:
            job.pending_series = series
            text, buttons = mediamgr.scope_question(job)
        else:
            await self._plan_series(job, series, acc)
            text, buttons = mediamgr.preview(job)
        if not buttons:
            return Final(text)
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Aufräumen", None), stage="mm", data=job)
        flow.step = Step(text, buttons)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _plan_series(self, job: Any, series: list[Any], acc: plex.Account | None) -> None:
        q = job.query
        if q.scope in ("", "series"):
            job.items += [mediamgr.whole(t) for t in series]
        else:
            for t in series:
                try:
                    eps = await self.sonarr.episodes(t.arr_id)
                    key = t.plex_keys.get(acc.user_id) if acc else None
                    leaves = await self.plex.episodes(acc, key) if (key and self.plex) else []
                except (ArrError, plex.PlexError) as e:
                    job.failed.append(f"{t.title}: {e}")
                    continue
                job.items += mediamgr.plan_parts(t, eps, leaves, q, acc.user_id if acc else None)
        job.items.sort(key=lambda i: -i.size)
        if q.largest:
            job.items = job.items[: q.largest]

    async def _mm_step(self, flow: Flow, value: str) -> Step:
        job = flow.data
        if value == "no":
            return Step(mediamgr.summary(job, cancelled=True), done=True)
        if value.startswith("sc:"):
            job.query.scope = value[3:]
            series, job.pending_series = job.pending_series, []
            await self._plan_series(job, series, self._plex_of(flow.chat_id))
            text, buttons = mediamgr.preview(job)
            return Step(text, buttons, done=not buttons)
        if value == "all":
            for item in job.items:
                err = await mediamgr.delete(self.radarr, self.sonarr, item)
                (job.failed.append(err) if err else job.deleted.append(item))
            return Step(mediamgr.summary(job), done=True)
        if value in ("del", "skip", "keep") and job.pos < len(job.items):
            item = job.items[job.pos]
            if value == "del":
                err = await mediamgr.delete(self.radarr, self.sonarr, item)
                (job.failed.append(err) if err else job.deleted.append(item))
            elif value == "keep" and self.cleanup is not None:
                title = item.name.split(" – ")[0].split(": ")[0]
                self.cleanup.keep(item.service, item.arr_id, title)
                job.kept.append(title)
                # the other parts of the same series are protected now, too
                job.items = job.items[: job.pos + 1] + [i for i in job.items[job.pos + 1:]
                                                        if (i.service, i.arr_id) != (item.service, item.arr_id)]
            else:
                job.skipped += 1
            job.pos += 1
        elif value == "one":
            job.pos = 0
        if value == "stop" or job.pos >= len(job.items):
            return Step(mediamgr.summary(job), done=True)
        text, buttons = mediamgr.one(job)
        return Step(text, buttons)

    async def _t_admin_storage(self, chat_id: int, top: int | None = None) -> Final:
        from . import extras

        report = await self._media_report()
        disks = await extras.disks(self.radarr, self.sonarr)
        acc = self._plex_of(chat_id)
        self._outbox.setdefault(chat_id, []).append(
            View(chat_id, "html", arg=mediamgr.storage_text(report, disks, acc.user_id if acc else None,
                                                        max(3, min(int(top or 10), 25)))))
        return Final("")

    async def _t_admin_keep(self, chat_id: int, title: str | None = None, kind: str | None = None,
                            off: bool = False) -> Final:
        if self.cleanup is None:
            raise ArrError("Nicht eingerichtet.")
        if not title:
            rows = self.cleanup.kept_titles()
            if not rows:
                return Final("🛡 Noch nichts geschützt. Z. B. „Dune nie löschen“.")
            return Final("🛡 Nie löschen:\n" + "\n".join(f"{'🎬' if s == 'radarr' else '📺'} {t}" for s, _, t in rows))
        kinds = [kind] if kind else [k for k, svc in (("movie", self.radarr), ("series", self.sonarr)) if svc]
        found, errors = [], []
        for k in kinds:
            try:
                found.append((k, await self._find_in_library(k, title)))
            except ArrError as e:
                errors.append(str(e))
        if not found:
            raise ArrError(errors[0] if errors else f"{title} nicht gefunden.")
        if len(found) > 1:
            raise ArrError(f"„{title}“ gibt es als Film und als Serie – welches meinst du?")
        k, lib_id = found[0]
        item = await self._svc(k).get(lib_id)
        service = "radarr" if k == "movie" else "sonarr"
        if off:
            self.cleanup.unkeep(service, lib_id)
            return Final(f"🔓 {item.get('title')} ist nicht mehr geschützt.")
        self.cleanup.keep(service, lib_id, item.get("title") or title)
        return Final(f"🛡 {item.get('title')} wird nie gelöscht – auch nicht von Aufräumregeln.")

    async def _t_admin_activity(self, chat_id: int, user: str | None = None, problems_only: bool = False,
                                days: int = 7) -> Final:
        who = self.users.find(user) if user else None
        if user and who is None:
            raise ArrError(f"Nutzer {user} nicht gefunden.")
        mode = "problems" if problems_only else "all"
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "activity", arg=(mode, max(1, min(days, 90)), who)))
        return Final("")

    async def _t_admin_stats(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "stats"))
        return Final("")

    async def _t_admin_users(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "users"))
        return Final("")

    async def _t_admin_check(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "check"))
        return Final("")

    async def _t_admin_set_limit(self, chat_id: int, who: str, kind: str, value: str | float | None = None) -> Final:
        try:
            return Final(self.users.apply_limit(who, kind, value))
        except ValueError as e:
            raise ArrError(str(e)) from e

    # ---- recipes: tools the bot builds for itself (admins) -------------------------------------
    @staticmethod
    def _recipe_cost_text(cost: float | None, tokens: int) -> str:
        money = f" ≈ {cost * 100:.2f} ct".replace(".", ",") if cost is not None else ""
        return f"{tokens:,} Tokens".replace(",", ".") + money

    async def _t_build_tool(self, chat_id: int, request: str) -> Final:
        if self.recipes is None or self.builder is None:
            raise ArrError("Werkzeuge bauen ist nicht eingerichtet.")
        inp, out = recipes.estimate(request)
        cost = self.builder_cost({"input": inp, "output": out})
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Werkzeug", None), stage="recipe",
                    data={"request": request})
        money = f" ≈ {cost * 100:.1f} ct".replace(".", ",") if cost is not None else ""
        tokens = f"{inp + out:,}".replace(",", ".")
        flow.step = Step(
            f"🛠 Dafür habe ich noch kein Werkzeug:\n„{request}“\n\n"
            f"Ich kann mir eins bauen lassen ({self.builder_model}): etwa {tokens} Tokens{money}, "
            "mit Korrekturversuch bis doppelt so viel. Danach kostet jede Nutzung nur die normale Frage.\n"
            "Das Werkzeug kann nur lesen (TMDB, Radarr, Sonarr). Du siehst einen Probelauf und entscheidest, "
            "ob es bleibt.",
            [(f"🛠 Bauen{money}", "build"), ("Abbrechen", "no")])
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    def _recipe_clients(self) -> dict:
        return {k: v for k, v in (("tmdb", self.tmdb), ("radarr", self.radarr), ("sonarr", self.sonarr)) if v}

    async def _recipe_step(self, flow: Flow, value: str) -> Step:
        d = flow.data
        if value == "keep" and d.get("recipe"):
            r = d["recipe"]
            r.created, r.tokens, r.cost = utcnow().isoformat(), d["tokens"], d["cost"] or 0.0
            self.recipes.save(r)  # type: ignore[union-attr]
            return Step(f"✅ Werkzeug „{r.title}“ gespeichert. Frag einfach wieder so, dann nehme ich es. "
                        "/rezepte zeigt alle.", done=True)
        if value != "build" or d.get("recipe"):
            return Step("👍 Okay, kein neues Werkzeug.", done=True)
        usage = {"input": 0, "cached": 0, "output": 0}
        error, raw, recipe, outcome = None, None, None, None
        for _ in range(2):  # one try to correct itself
            try:
                recipe, example, u = await recipes.build(self.builder, d["request"], error, raw)
                for k in usage:
                    usage[k] += u.get(k, 0) or 0
                raw = asdict(recipe)
                outcome = await recipes.run(recipe, example, self._recipe_clients())
                if outcome.lines:
                    break
                error = "Der Probelauf hat nichts gefunden – Filter, Pfad oder items prüfen."
            except recipes.RecipeError as e:
                for k in usage:
                    usage[k] += (getattr(e, "usage", None) or {}).get(k, 0) or 0
                raw = getattr(e, "raw", raw)
                error, recipe, outcome = str(e), None, None
            except (ArrError, TmdbError) as e:
                error, outcome = f"Abfrage fehlgeschlagen: {e}", None
            except Exception as e:  # noqa: BLE001 - the model gave something odd
                log.exception("Rezept bauen")
                error, outcome = f"{type(e).__name__}: {e}", None
        tokens = usage["input"] + usage["output"]
        cost = self.builder_cost(usage)
        if self.on_llm_usage and tokens:
            self.on_llm_usage(flow.chat_id, usage, cost)
        spent = self._recipe_cost_text(cost, tokens)
        if recipe is None or outcome is None or not outcome.lines:
            return Step(f"❌ Das Werkzeug hat nicht geklappt: {error}\n💰 Verbraucht: {spent}", done=True)
        d.update(recipe=recipe, tokens=tokens, cost=cost)
        preview = "\n".join(outcome.lines[:8]) + ("\n…" if len(outcome.lines) > 8 else "")
        params = ", ".join(recipe.params) or "keine"
        return Step(f"🛠 Neues Werkzeug: {recipe.title}\n{recipe.description}\nEinstellbar: {params}\n\n"
                    f"Probelauf:\n{outcome.heading}\n{preview}\n\n💰 Bau: {spent}",
                    [("✅ Behalten", "keep"), ("🗑 Verwerfen", "no")])

    async def run_recipe(self, chat_id: int, name: str, args: dict) -> Final:
        r = self.recipes.get(name) if self.recipes else None
        if r is None:
            raise ArrError("Dieses Werkzeug gibt es nicht mehr.")
        try:
            out = await recipes.run(r, args, self._recipe_clients())
        except recipes.RecipeError as e:
            raise ArrError(str(e)) from e
        r.uses += 1
        self.recipes.save(r)  # type: ignore[union-attr]
        if not out.lines:
            return Final(f"{out.heading}\n🤷 Nichts gefunden.")
        text = "\n".join([out.heading, *[f"{i}. {ln}" for i, ln in enumerate(out.lines, 1)]])
        cands = await self._recipe_cands(r, out.items) if r.offer else []
        if cands:
            self._offer(chat_id, cands, text + "\n\nTippe einen Titel an, um ihn hinzuzufügen.", bulk=True)
            return Final("")
        return Final(text)

    async def _recipe_cands(self, r: recipes.Recipe, items: list) -> list[Candidate]:
        svc = self.radarr if r.offer == "movie" else self.sonarr
        owned = await self._library_ids([r.offer]) if svc else None
        out = []
        for i in items:
            tid = recipes.dig(i, r.id_field)
            if not isinstance(tid, int):
                continue
            title = recipes.dig(i, "title") or recipes.dig(i, "name") or "?"
            day = str(recipes.dig(i, "release_date") or recipes.dig(i, "first_air_date") or "")
            year = int(day[:4]) if day[:4].isdigit() else None
            if r.offer == "movie":
                if owned and ("movie", tid) in owned["library"]:
                    continue
                out.append(Candidate("movie", tid, title, year, tmdb_id=tid))
            elif self.tmdb:
                try:
                    tvdb = await self.tmdb.tvdb_from_tv(tid)
                except TmdbError:
                    continue
                if tvdb and not (owned and tvdb in owned["tvdb"]):
                    out.append(Candidate("series", tvdb, title, year, tmdb_id=tid))
        return out

    async def _t_admin_recipes(self, chat_id: int, action: str = "list", name: str | None = None) -> Final:
        if self.recipes is None:
            raise ArrError("Werkzeuge bauen ist nicht eingerichtet.")
        if action == "delete":
            want = (name or "").lower().removeprefix("recipe_")
            r = next((x for x in self.recipes.all() if want in (x.name, x.title.lower())), None)
            if r is None or not self.recipes.remove(r.name):
                raise ArrError(f"Werkzeug „{name}“ gibt es nicht.")
            return Final(f"🗑 Werkzeug „{r.title}“ gelöscht.")
        lines = [f"• {r.title} ({r.tool}) – {r.uses}× benutzt" for r in self.recipes.all()]
        return Final("🛠 Selbstgebaute Werkzeuge:\n" + "\n".join(lines) if lines
                     else "🛠 Noch keine selbstgebauten Werkzeuge.")

    async def _t_mute_notifications(self, chat_id: int, minutes: int | None = None, off: bool = False,
                                    hours: int | None = None, days: int | None = None) -> Final:
        total = (minutes or 0) + (hours or 0) * 60 + (days or 0) * 1440
        return Final(mute(self.store, chat_id, None if off else (total or 60)))

    async def _t_notification_settings(self, chat_id: int, start: bool | None = None, done: bool | None = None,
                                       problem: bool | None = None, nothing: bool | None = None) -> Final:
        from . import notify

        notify.change(self.store, chat_id, start=start, done=done, problem=problem, nothing=nothing)
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "notify"))
        return Final("")

    async def _t_bot_info(self, chat_id: int) -> Final:
        if self.bot_info is None:
            raise ArrError("Keine Versionsinfo verfügbar.")
        return Final(await self.bot_info(chat_id))

    async def _t_admin_overview(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "admin"))
        return Final("")

    async def _t_admin_user_info(self, chat_id: int, who: str) -> Final:
        uid = self.users.find(str(who)) if self.users else None
        if uid is None or uid not in self.users.users:
            raise ArrError(f"Nutzer „{who}“ kenne ich nicht. /nutzer zeigt alle.")
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "usercard", arg=uid))
        return Final("")

    async def _t_admin_watch_questions(self, chat_id: int, on: bool | None = None, every_days: int | None = None,
                                       max: int | None = None, after_days: int | None = None,
                                       now: bool = False) -> Final:
        if self.cleanup is None:
            raise ArrError("Nicht eingerichtet.")
        s = watchask.load(self.cleanup)
        changed = False
        for attr, val, lo, hi in (("every", every_days, 1, 60), ("max", max, 1, 10), ("after", after_days, 0, 90)):
            if val is not None:
                setattr(s, attr, min(hi, builtins_max(lo, int(val))))
                changed = True
        if on is not None:
            s.on, changed = bool(on), True
        if changed:
            watchask.save(self.cleanup, s)
        if not now:
            return Final(s.text())
        n = await self.watch_questions(chat_id)
        return Final("" if n else "👍 Gerade nichts: Kein Titel, den du gesehen hast und nach dem ich noch nicht gefragt habe.")

    async def watch_questions(self, chat_id: int) -> int:
        """Questions about titles the admin saw in Plex ("Dune gesehen – löschen?"), into the outbox."""
        acc = self.plex_store.get(chat_id) if self.plex_store else None
        if not (self.cleanup and self.plex and acc and (self.radarr or self.sonarr)):
            return 0
        s = watchask.load(self.cleanup)
        now = utcnow()
        report = await self._media_report()
        movies, series = watchask.candidates(report, acc.user_id, self.cleanup.kept(), self._recent_requests(),
                                             s.after, now)
        titles = sorted(movies + series, key=lambda t: t.viewed_by.get(acc.user_id) or now, reverse=True)
        asks: list[watchask.Ask] = []
        for t in titles:
            if len(asks) >= s.max:
                break
            if t.kind == "movie":
                a: watchask.Ask | None = watchask.Ask(t, [mediamgr.whole(t)])
            else:
                if not self.sonarr:
                    continue
                try:
                    eps = await self.sonarr.episodes(t.arr_id)
                    key = t.plex_keys.get(acc.user_id)
                    leaves = await self.plex.episodes(acc, key) if key else []
                except (ArrError, plex.PlexError) as e:
                    log.info("Nachfrage %s: %s", t.title, e)
                    continue
                a = watchask.series_ask(t, eps, leaves, acc.user_id, now)
            if a is not None and not watchask._answered(self.cleanup, a.key, now):
                asks.append(a)
        for a in asks:
            flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate(a.title.kind, 0, a.title.title, None), stage="wa",
                        data=a)
            flow.step = Step(*watchask.question(a))
            self.flows[flow.id] = flow
            self._outbox.setdefault(chat_id, []).append(flow)
        return len(asks)

    async def _wa_step(self, flow: Flow, value: str) -> Step:
        a = flow.data
        t = a.title
        self.flows.pop(flow.id, None)
        if value in ("del", "whole"):
            items = [a.whole] if value == "whole" and a.whole is not None else a.items
            errors = [e for i in items if (e := await mediamgr.delete(self.radarr, self.sonarr, i))]
            watchask.answer(self.cleanup, a.key, "deleted")
            if errors:
                return Step("⚠️ Nicht gelöscht: " + "; ".join(errors[:3]), done=True)
            freed = sum(i.size for i in items)
            from .media import size_text

            return Step(f"🗑 Gelöscht ({size_text(freed)} frei). Gesperrt ist nichts – einfach wieder hinzufügen, "
                        "wenn du es doch nochmal sehen willst.", done=True)
        if value == "keep":
            self.cleanup.keep(t.service, t.arr_id, t.title)
            return Step(f"🛡 {t.title} wird nie gelöscht – ich frage auch nicht mehr.", done=True)
        if value == "later":
            watchask.answer(self.cleanup, a.key, "later")
            return Step(f"⏰ Okay, ich frage in {watchask.LATER_DAYS} Tagen nochmal.", done=True)
        watchask.answer(self.cleanup, a.key, "no")
        return Step("➡️ Bleibt. Dazu frage ich nicht mehr" + (" (erst wieder bei neuen gesehenen Staffeln)."
                                                             if t.kind == "series" else "."), done=True)

    async def _t_admin_limit_advice(self, chat_id: int) -> Final:
        from . import limitadvice

        a = limitadvice.advise(self.users)
        text = limitadvice.render(a, self.users)
        if not a.total:
            return Final(text)
        buttons = [("✅ Alles übernehmen", "all"), ("Nur Gesamt", "total")]
        if a.standard:
            buttons.append(("Nur Standard pro Nutzer", "standard"))
        buttons.append(("✖️ Nein danke", "no"))
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Limits", None), stage="adv", data=a)
        flow.step = Step(text, buttons)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _t_admin_permissions(self, chat_id: int, who: str, grant: list[str] | None = None,
                                   revoke: list[str] | None = None) -> Final:
        try:
            text = self.users.apply_perms(who, grant, revoke)
        except ValueError as e:
            raise ArrError(str(e)) from e
        uid = self.users.find(who)
        if (grant or revoke) and uid and self.notify_user and uid != chat_id:
            u = self.users.users.get(uid)
            if u is not None:
                await self.notify_user(uid, users_mod.perms_text(u).replace(f"🔐 {u.name} darf", "🔐 Du darfst"))
        return Final(text)

    async def _t_admin_max_quality(self, chat_id: int, who: str, quality: str) -> Final:
        try:
            return Final(self.users.apply_quality(who, quality))
        except ValueError as e:
            raise ArrError(str(e)) from e

    async def _t_admin_user(self, chat_id: int, who: str, action: str) -> Final:
        try:
            uid, text = self.users.apply_role(who, action)
        except ValueError as e:
            raise ArrError(str(e)) from e
        if self.notify_user and action == "freigeben":
            await self.notify_user(uid, "🎉 Du bist freigeschaltet! Schreib mir einfach, was du sehen willst.")
        return Final(text)

    async def _german_titles(self, kind: str, tmdb_ids: list[int | None]) -> dict[int, str]:
        ids = [i for i in tmdb_ids if i]
        if not self.tmdb or not ids:
            return {}
        return await self.tmdb.german_titles(kind, ids)

    async def _t_german_info(self, chat_id: int, kind: str, id: int, season: int | None = None,  # noqa: A002
                             indexer: bool | None = None) -> Final:
        """Rendered in code: the AI only picks the title, it never paraphrases dates (that went wrong)."""
        today = date.today()
        if kind == "movie":
            if not self.tmdb:
                return Final(NO_TMDB)
            text = await german.movie_report(self._need(self.radarr, "Radarr"), self.tmdb, int(id),
                                             bool(indexer), today)
        else:
            # Series: checking the indexers is the only real proof of German episodes, so it is on by default.
            text = await german.series_report(self._need(self.sonarr, "Sonarr"), self.tmdb, int(id),
                                              int(season) if season else None, indexer is not False, today)
        return Final(text)

    async def _t_suggest(self, chat_id: int, kind: str | None = None, from_year: int | None = None,
                         to_year: int | None = None, min_rating: float | None = None, mood: str | None = None,
                         genres: list[str] | None = None, like: str | None = None, max_minutes: int | None = None,
                         german_only: bool = False, calibrate: bool = False) -> Final | dict:
        wanted = recommend.wanted_genres(mood, genres)

        def fits(r: Any) -> bool:
            if (r.kind, r.tmdb_id) in skip or avoid.intersection(r.genres):
                return False
            if wanted and not wanted.intersection(r.genres):
                return False
            if (from_year or to_year) and not r.year:
                return False
            return ((not from_year or r.year >= int(from_year)) and (not to_year or r.year <= int(to_year))
                    and (not min_rating or r.rating >= float(min_rating)))

        def wish_text() -> str:
            return " ".join(x for x in (
                (mood or "").strip() or ", ".join(sorted(wanted)),
                f"ab {from_year}" if from_year and not to_year else "",
                f"bis {to_year}" if to_year and not from_year else "",
                f"{from_year}–{to_year}" if from_year and to_year else "",
                f"ab ⭐ {float(min_rating):g}".replace(".", ",") if min_rating else "",
                f"bis {int(max_minutes)} Min." if max_minutes else "",
                "auf Deutsch" if german_only else "") if x)

        kinds = [kind] if kind else [k for k, svc in (("movie", self.radarr), ("series", self.sonarr)) if svc]
        history = [r for r in reversed(self.store.state.requests) if r["chat_id"] == chat_id and r["kind"] in kinds]
        if not self.tmdb:
            # Without TMDB the model suggests from its own knowledge, then searches as usual.
            return {
                "bisher": [r["title"] for r in history[:15]] or "nichts",
                "hinweis": "Nenne 3-5 passende Titel (deutsche Titel), suche sie und biete sie per offer_titles an."
                + (f" Nur: {wish_text()}." if wish_text() else "") + (f" Ähnlich wie: {like}." if like else ""),
            }
        prof = self.profiles.profile(chat_id) if self.profiles else None
        explicit = bool(wanted or from_year or to_year or min_rating or max_minutes or german_only or like)
        if prof:  # defaults the user told the bot once ("nur Filme ab 2000")
            from_year = from_year or prof.prefs.from_year
            min_rating = min_rating or prof.prefs.min_rating
        avoid = set(prof.prefs.avoid) if prof else set()
        skip = (prof.rejected | prof.seen) if prof else set()
        owned = await self._library_ids(kinds)

        like_title = None
        if like:
            hit = await self._resolve_title(kind, like)
            if hit is None:
                return Final(f"🤷 „{like}“ habe ich nicht gefunden – wie heißt der Titel genau (Film oder Serie)?")
            like_title = hit["title"]
            kinds = [hit["kind"]]
            seeds = [(hit["kind"], hit["tmdb"], hit["title"])]
        else:
            personal = ([(k, t, title) for k, t, title, _ in self.profiles.seeds(chat_id, 10) if k in kinds]
                        if self.profiles else [])
            if not personal:
                personal = [(r["kind"], r.get("tmdb"), r["title"]) for r in history if r.get("tmdb")]
            if self.profiles and (calibrate or (not personal and not explicit
                                                and not self.profiles.asked(chat_id))):
                return await self._calibrate(chat_id, kinds)
            seeds = list(dict.fromkeys(personal))[:8]
            if len(seeds) < 3:  # little history: the shared library shows the household's taste
                seeds += [s for s in owned["recent"] if s not in seeds][: 8 - len(seeds)]

        pool: dict[tuple[str, int], Any] = {}
        on_server: dict[tuple[str, int], Any] = {}  # on the server, but this user has not seen it yet
        results = await asyncio.gather(
            *(self.tmdb.recommendations(k, tid) for k, tid, _ in seeds), return_exceptions=True
        )
        for (k, _tid, title), res in zip(seeds, results):
            if isinstance(res, BaseException):
                continue
            for rank, r in enumerate(res[:20]):
                key = (k, r.tmdb_id)
                if key in owned["tmdb"]:
                    if key in owned["server"] and fits(r):
                        tip = on_server.setdefault(key, r)
                        tip.score += (20 - rank) / 20
                        if title not in tip.because:
                            tip.because.append(title)
                    continue
                if not fits(r):
                    continue
                cur = pool.setdefault(key, r)
                cur.score += (20 - rank) / 20
                if title not in cur.because:
                    cur.because.append(title)
        if like_title and pool:
            intro = f"💡 Wie {like_title}:"
        elif pool and (len(pool) >= 3 or not explicit):
            reasons = list(dict.fromkeys(t for _, _, t in seeds))[:3]
            intro = f"💡 Das könnte dir gefallen – weil du {', '.join(reasons)} magst:"
        elif wanted or max_minutes or min_rating:
            # The recommendations have too little of it: well rated titles of the wanted genres.
            for k in kinds:
                try:
                    found = await self.tmdb.discover(k, recommend.genre_ids(k, wanted), max_minutes,
                                                     from_year, to_year, min_rating)
                except TmdbError:
                    continue
                for r in found[:20]:
                    if (k, r.tmdb_id) not in owned["tmdb"] and fits(r):
                        pool.setdefault((k, r.tmdb_id), r)
            intro = "🎯 Passend zu deinem Wunsch:"
        elif from_year or to_year:
            # Too few matches in the recommendations: the best rated titles of the wanted years.
            start = date(int(from_year or 1900), 1, 1)
            end = date(int(to_year), 12, 31) if to_year else date.today()
            for k in kinds:
                for r in (await self.tmdb.top(k, start, end, "rating"))[:20]:
                    if (k, r.tmdb_id) not in owned["tmdb"] and fits(r):
                        pool.setdefault((k, r.tmdb_id), r)
            intro = "🏆 Gut bewertet aus der Zeit:"
        else:
            for k in kinds:
                for r in (await self.tmdb.trending(k))[:10]:
                    if (k, r.tmdb_id) not in owned["tmdb"] and fits(r):
                        pool.setdefault((k, r.tmdb_id), r)
            intro = "🔥 Gerade beliebt in Deutschland:"
        if wish_text():
            intro = intro.rstrip(":") + f" ({wish_text()}):"
        ranked = sorted(pool.values(), key=lambda r: recommend.score(r, prof.genre_bonus(r.genres) if prof else 0.0),
                        reverse=True)

        # Runtime and film series of the best ones: "unter 90 Minuten", not three parts of one series.
        collections: dict[tuple[str, int], int] = {}
        top = ranked[:16]
        infos = await asyncio.gather(*(self.tmdb.details(r.kind, r.tmdb_id) for r in top), return_exceptions=True)
        too_long = set()
        for r, info in zip(top, infos):
            if isinstance(info, BaseException):
                continue
            if info.get("collection"):
                collections[(r.kind, r.tmdb_id)] = info["collection"]
            if max_minutes and info.get("runtime") and info["runtime"] > int(max_minutes):
                too_long.add((r.kind, r.tmdb_id))
        ranked = [r for r in ranked if (r.kind, r.tmdb_id) not in too_long]
        order = recommend.diversify(ranked, MAX_SUGGESTIONS, collections)
        order += [r for r in ranked if r not in order]

        cands: list[Candidate] = []
        lines: list[str] = []
        checked = 0
        for r in order:
            if len(cands) >= MAX_SUGGESTIONS:
                break
            if german_only:
                if checked >= 18:
                    break
                checked += 1
                try:
                    if not recommend.german_available(await self.tmdb.german_info(r.kind, r.tmdb_id)):
                        continue
                except TmdbError:
                    continue
            if r.kind == "movie":
                c = Candidate("movie", r.tmdb_id, r.title, r.year)
            else:
                try:
                    tvdb = await self.tmdb.tvdb_from_tv(r.tmdb_id)
                except TmdbError:
                    continue
                if not tvdb or tvdb in owned["tvdb"]:
                    continue
                c = Candidate("series", tvdb, r.title, r.year)
            c.rating = r.rating
            c.tmdb_id, c.genres = r.tmdb_id, r.genres  # for "nichts davon" / 👎
            self._remember(chat_id, c)
            cands.append(c)
            why = recommend.reason(r) if not like_title else ("💎 Geheimtipp" if "💎 Geheimtipp" in r.because else "")
            if why:
                lines.append(f"• {r.title}: {why}")
        tips = sorted(on_server.values(), key=lambda r: recommend.score(r), reverse=True)[:2]
        tip_lines = [f"📼 Schon auf dem Server: {r.title}" + (f" ({r.year})" if r.year else "")
                     + (f" – wie {r.because[0]}" if r.because else "") for r in tips]
        if not cands:
            if tip_lines:
                return Final("Neues habe ich gerade nicht, aber:\n" + "\n".join(tip_lines))
            if explicit:
                return Final(f"🤷 Dazu ({wish_text() or like}) finde ich gerade nichts Neues. "
                             "Magst du den Wunsch etwas lockern?")
            return Final("🤷 Mir fällt gerade nichts Neues ein – ihr habt schon alles Passende. 😄")
        text = "\n".join([intro, *lines, *([""] + tip_lines if tip_lines else []), "",
                          "Tippe einen Titel an, um ihn hinzuzufügen. 👎/👁 unter „Bewerten“."])
        self._offer(chat_id, cands, text, suggest=True)
        return Final("")

    async def _calibrate(self, chat_id: int, kinds: list[str]) -> Final:
        """Quick start for someone the bot knows nothing about: well known titles to 👍/👎."""
        pool: list[Any] = []
        for k in kinds:
            try:
                pool += (await self.tmdb.trending(k))[:15]
            except TmdbError:
                continue
        pool.sort(key=lambda r: r.votes, reverse=True)
        cands: list[Candidate] = []
        for r in recommend.diversify(pool, 8):
            if r.kind == "movie":
                c = Candidate("movie", r.tmdb_id, r.title, r.year)
            else:
                try:
                    tvdb = await self.tmdb.tvdb_from_tv(r.tmdb_id)
                except TmdbError:
                    continue
                if not tvdb:
                    continue
                c = Candidate("series", tvdb, r.title, r.year)
            c.tmdb_id, c.genres, c.rating = r.tmdb_id, r.genres, r.rating
            cands.append(c)
            self._learn_cand(chat_id, "asked", c)  # shown once; never again as quick start
        if not cands:
            return Final("🤷 Gerade bekomme ich keine Titel von TMDB. Versuch es später nochmal.")
        offer = self._offer(chat_id, cands, "👋 Kurz zum Kennenlernen, damit meine Tipps passen:\n"
                            "Tippe 👍 bei Titeln, die dir gefallen haben, 👎 bei denen, die nicht dein Ding "
                            "sind. Unbekannte einfach auslassen – dann „Fertig“.", suggest=True)
        offer.calibrate = True
        return Final("")

    async def _resolve_title(self, kind: str | None, title: str) -> dict | None:
        """A title the user named ("wie Inception") -> kind, tmdb id, German title, genres."""
        kinds = [kind] if kind else ["movie", "series"]
        best = None
        for k in kinds:
            svc = self.radarr if k == "movie" else self.sonarr
            if not svc:
                continue
            try:
                res = await svc.lookup(title)
            except ArrError:
                continue
            res = [x for x in res if x.get("tmdbId")]
            if not res:
                continue
            exact = next((x for x in res if self._norm(x.get("title") or "") == self._norm(title)), None)
            hit = exact or res[0]
            item = {"kind": k, "tmdb": hit["tmdbId"], "tvdb": hit.get("tvdbId") if k == "series" else None,
                    "title": hit.get("title") or title, "year": hit.get("year") or None,
                    "genres": hit.get("genres") or [], "exact": exact is not None}
            if best is None or (item["exact"] and not best["exact"]):
                best = item
        return best

    def _learn_cand(self, chat_id: int, action: str, c: Any) -> None:
        if not self.profiles:
            return
        tmdb_id = c.ext_id if c.kind == "movie" else c.tmdb_id
        self.profiles.learn(chat_id, action, c.kind, c.title, tmdb=tmdb_id,
                            tvdb=c.ext_id if c.kind == "series" else None, year=c.year, genres=c.genres)

    # ---- feedback on titles (👍/👎/👁, ratings) ---------------------------------------------------
    def owner_ok(self, kind: str, obj_id: str, uid: int, chat_id: int) -> bool:
        """A button may only be pressed in the chat it was sent to, by someone allowed there. Unknown ids
        (expired) are fine: the handlers answer "abgelaufen" themselves."""
        obj = (self.flows if kind == "flow" else self.offers).get(obj_id)
        if obj is None:
            return True
        if obj.chat_id != chat_id:
            return False
        stage = getattr(obj, "stage", "")
        if stage in ("mm", "dup"):  # media manager: the right is needed again at the moment of deleting
            return self.may(uid, "admin_delete_media")
        if stage == "remove":
            return self.may(uid, "remove_title")
        if stage == "wa":
            return self.may(uid, "admin_delete_media")
        if stage == "adv":
            return self._admin(uid) and self.users is not None and self.is_admin(uid)
        return True

    def offer_mode(self, offer_id: str, mode: str) -> Offer | None:
        offer = self.offers.get(offer_id)
        if offer is not None:
            offer.mode = mode
        return offer

    def offer_feedback(self, offer_id: str, idx: int, action: str, user_id: int) -> tuple[Offer, str, str | None] | None:
        """👍 (l), 👎 (d) or 👁 seen (s) on one title of a suggestion list. Returns the offer, a short
        answer and for 👁 the id of a rating question ("wie fandest du's?")."""
        offer = self.offers.get(offer_id)
        if offer is None or not 0 <= idx < len(offer.cands) or action not in ("l", "d", "s"):
            return None
        c = offer.cands[idx]
        if idx not in offer.hidden:
            offer.hidden.append(idx)
        if action == "s":
            return offer, f"👁 {c.title}", self.ask_rating(user_id, c)
        self._learn_cand(user_id, "liked" if action == "l" else "disliked", c)
        return offer, (f"👍 {c.title}" if action == "l" else f"👎 {c.title} – schlage ich nicht mehr vor"), None

    def ask_rating(self, user_id: int, c: Any) -> str:
        """Remember a question "wie fandest du X?" (buttons ⭐1–5). Returns its id."""
        ask_id = uuid.uuid4().hex[:10]
        self.rating_asks[ask_id] = c
        while len(self.rating_asks) > 300:
            self.rating_asks.pop(next(iter(self.rating_asks)))
        self._learn_cand(user_id, "asked", c)
        return ask_id

    def rate(self, ask_id: str, stars: int, user_id: int) -> str | None:
        c = self.rating_asks.pop(ask_id, None)
        if c is None:
            return None
        if stars >= 4:
            self._learn_cand(user_id, "liked", c)
            return f"👍 {c.title}: {'⭐' * stars} – gemerkt, davon suche ich mehr."
        if 1 <= stars <= 2:
            self._learn_cand(user_id, "disliked", c)
            return f"👎 {c.title}: {'⭐' * stars} – gemerkt, sowas schlage ich seltener vor."
        return f"👌 {c.title}: gemerkt." if stars == 3 else "👌 Alles klar."

    async def _t_title_feedback(self, chat_id: int, title: str, opinion: str, kind: str | None = None) -> Final:
        hit = await self._resolve_title(kind, title)
        if hit is None:
            return Final(f"🤷 „{title}“ habe ich nicht gefunden – Film oder Serie, und wie heißt er genau?")
        action = {"liked": "liked", "disliked": "disliked", "seen": "asked"}.get(opinion, "asked")
        if self.profiles:
            self.profiles.learn(chat_id, action, hit["kind"], hit["title"], tmdb=hit["tmdb"], tvdb=hit["tvdb"],
                                year=hit["year"], genres=hit["genres"])
        return Final({"liked": f"👍 Gemerkt: {hit['title']} gefällt dir – davon suche ich mehr.",
                      "disliked": f"👎 Gemerkt: {hit['title']} ist nicht deins.",
                      }.get(opinion, f"👁 Gemerkt: {hit['title']} kennst du schon – schlage ich nicht vor."))

    # ---- user profile --------------------------------------------------------------------------
    def _learn(self, chat_id: int, action: str, kind: str, item: dict, title: str | None = None) -> None:
        if not self.profiles:
            return
        name = title or (f"{item.get('title')} ({item.get('year')})" if kind == "movie" and item.get("year")
                         else item.get("title", "?"))
        self.profiles.learn(chat_id, action, kind, name, tmdb=item.get("tmdbId"),
                            tvdb=item.get("tvdbId") if kind == "series" else None,
                            year=item.get("year") or None, genres=item.get("genres") or [])

    def reject_offer(self, offer_id: str, chat_id: int) -> str | None:
        """'Nichts davon' under suggestions: never show them again, their genres count a bit less."""
        offer = self.offers.pop(offer_id, None)
        if offer is None:
            return None
        for i, c in enumerate(offer.cands):
            if i not in offer.hidden:
                self._learn_cand(chat_id, "not_interested", c)
        return "👍 Verstanden, die schlage ich nicht mehr vor. Frag einfach nochmal nach neuen Ideen."

    async def _t_remember(self, chat_id: int, like: list[str] | None = None, avoid: list[str] | None = None,
                          from_year: int | None = None, min_rating: float | None = None, note: str | None = None,
                          forget: list[str] | None = None) -> Final:
        if not self.profiles:
            return Final("Profile sind nicht eingerichtet.")
        return Final(self.profiles.remember(chat_id, like=like, avoid=avoid, from_year=from_year,
                                            min_rating=min_rating, note=note, forget=forget))

    async def _t_calendar(self, chat_id: int, days: int | None = None, mine: bool = False) -> Final:
        days = max(1, min(int(days or 7), 31))
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "calendar", arg=(days, bool(mine))))
        return Final("")

    async def _t_disk_space(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "disk"))
        return Final("")

    async def _t_notify_german(self, chat_id: int, kind: str, id: int, season: int | None = None,  # noqa: A002
                               stop: bool = False) -> Final:
        if not self.profiles:
            return Final("Profile sind nicht eingerichtet.")
        if stop:
            gone = [w for w in self.profiles.watches(chat_id) if w["kind"] == kind and w["ext_id"] == int(id)]
            for w in gone:
                self.profiles.drop_watch(w["id"])
            return Final(f"🔕 Okay, für {gone[0]['title']} melde ich mich nicht mehr." if gone
                         else "Dafür hatte ich keine Benachrichtigung.")
        if kind == "movie" and not self.tmdb:
            return Final(NO_TMDB)
        cand = await self._candidate(chat_id, kind, int(id))
        if kind == "series" and not cand.lib_id:
            return Final(f"📺 {cand.title} ist noch nicht in Sonarr. Füg die Serie zuerst hinzu, dann prüfe ich "
                         "täglich, ob es neue deutsche Folgen gibt.")
        title = cand.title + (f" ({cand.year})" if kind == "movie" and cand.year else "")
        what = f"{title} Staffel {season}" if season else title
        self.profiles.add_watch(chat_id, kind, int(id), title, int(season) if season else None)
        w = [x for x in self.profiles.watches(chat_id) if x["kind"] == kind and x["ext_id"] == int(id)
             and x["season"] == (int(season) if season else None)]
        notices = await watch.check_watches(self, only=w)  # maybe it's already there
        if notices:
            n = notices[0]
            return Final(n.text) if n.text else Final("")
        return Final(f"🔔 Mach ich! Ich prüfe {what} jeden Tag und melde mich, sobald es "
                     f"{'eine deutsche Fassung gibt' if kind == 'movie' else 'neue deutsche Folgen gibt'}.")

    async def _t_weekly_digest(self, chat_id: int, day: str | None = None, off: bool = False) -> Final:
        if not self.profiles:
            return Final("Profile sind nicht eingerichtet.")
        if off:
            self.profiles.set_digest(chat_id, None)
            return Final("🔕 Okay, kein Wochenrückblick mehr.")
        n = watch.parse_day(day) if day is not None else 6
        if n is None:
            return Final("An welchem Wochentag? Z. B. „jeden Sonntag“.")
        self.profiles.set_digest(chat_id, n)
        return Final(f"📰 Gern! Du bekommst jeden {profiles.DAY_NAMES[n]} um 10 Uhr deinen Wochenrückblick: "
                     "was fertig wurde, was in der Woche kommt, und ein paar Vorschläge.")

    async def _t_plex_status(self, chat_id: int, title: str | None = None, link: bool = False) -> Final:
        if not (self.plex and self.plex_store):
            return Final("Plex ist nicht eingerichtet.")
        acc = self.plex_store.get(chat_id)
        if link or not acc:
            self._outbox.setdefault(chat_id, []).append(View(chat_id, "plex_link"))
            return Final("" if link else "🔗 Dafür verbinde zuerst dein Plex-Konto:")
        try:
            if not title:
                deck = await self.plex.on_deck(acc)
                self._outbox.setdefault(chat_id, []).append(
                    View(chat_id, "plex", arg={"html": plex.on_deck_text(deck)}))
                return Final("")
            found = await self.plex.search(acc, title)
            if not found:
                return Final(f"🤷 „{title}“ habe ich in Plex nicht gefunden. Noch nicht fertig geladen? /downloads")
            it = found[0]
            meta = await self.plex.metadata(acc, it.rating_key)
            eps = await self.plex.episodes(acc, it.rating_key) if it.kind == "series" else []
        except plex.PlexError as e:
            return Final(f"⚠️ {e}")
        text, key = plex.progress_text(it, meta, eps)
        url = self.plex.web_link(acc, key) if key else None
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "plex", arg={"text": text, "url": url}))
        return Final("")

    async def _t_my_profile(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "profile"))
        return Final("")

    async def import_history(self) -> int:
        """First start with profiles: learn from the old request list (genres from the library)."""
        if not self.profiles or self.profiles.has_events() or not self.store.state.requests:
            return 0
        lib: dict[tuple[str, int], dict] = {}
        for kind, svc, key in (("movie", self.radarr, "tmdbId"), ("series", self.sonarr, "tvdbId")):
            if svc:
                try:
                    lib.update({(kind, i[key]): i for i in await svc.library() if i.get(key)})
                except ArrError as e:
                    log.warning("Profil-Import: %s", e)
        for r in self.store.state.requests:
            item = lib.get((r["kind"], r.get("tmdb") if r["kind"] == "movie" else r.get("tvdb")), {})
            self.profiles.learn(r["chat_id"], "requested", r["kind"], r["title"], tmdb=r.get("tmdb"),
                                tvdb=r.get("tvdb"), year=item.get("year"), genres=item.get("genres") or [])
        return len(self.store.state.requests)

    async def _t_top_titles(
        self, chat_id: int, kind: str, year: int | None = None, month: int | None = None, count: int = 5,
        sort: str = "popular", download: bool = False, studio: str | None = None,
    ) -> Final:
        if not self.tmdb:
            return Final(NO_TMDB)
        self._svc(kind)
        count = max(1, min(int(count or 5), MAX_TOP))
        start: date | None = None
        end: date | None = None
        period = ""
        if year and month:
            year, month = int(year), int(month)
            start = date(year, month, 1)
            end = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
            period = f"{MONTHS[month - 1]} {year}"
        elif year:
            start, end, period = date(int(year), 1, 1), date(int(year), 12, 31), str(year)
        companies = None
        if studio:
            companies = await self.tmdb.companies(studio)
            if not companies:
                return Final(f"🤷 Ein Studio „{studio}“ kenne ich bei TMDB nicht.")
        recs = await self.tmdb.top(kind, start, end, sort, companies)
        owned = await self._library_ids([kind])

        cands: list[tuple[Candidate, bool]] = []
        for r in recs:
            if len(cands) >= count:
                break
            if kind == "movie":
                c, have = Candidate("movie", r.tmdb_id, r.title, r.year), ("movie", r.tmdb_id) in owned["library"]
            else:
                try:
                    tvdb = await self.tmdb.tvdb_from_tv(r.tmdb_id)
                except TmdbError:
                    continue
                if not tvdb:
                    continue
                c, have = Candidate("series", tvdb, r.title, r.year), tvdb in owned["tvdb"]
            c.rating = r.rating
            self._remember(chat_id, c)
            cands.append((c, have))
        if not cands:
            return Final(f"🤷 Für {period or studio or 'diese Suche'} habe ich bei TMDB keine Daten gefunden.")

        what = "Filme" if kind == "movie" else "Serien"
        how = {"rating": "bestbewertet", "revenue": "nach Einspielergebnis"}.get(sort, "meistgesehen")
        label = f"{studio}-{what}" if studio else what
        head = f"🏆 Top {len(cands)} {label}{' ' + period if period else ''} ({how}):"
        if not download:
            lines = [f"{i}. {c.title}" + (f" ({c.year})" if c.year else "")
                     + (f" ⭐ {c.rating:.1f}".replace(".", ",") if c.rating else "") + (" ✅ schon da" if have else "")
                     for i, (c, have) in enumerate(cands, 1)]
            new = [c for c, have in cands if not have]
            if not new:
                return Final("\n".join([head, *lines, "", "👍 Die habt ihr schon alle."]))
            hint = "Tippe einen Titel an, um ihn hinzuzufügen."
            if len(new) < len(cands):
                hint += " Was schon da ist, hat keinen Button."
            self._offer(chat_id, new, "\n".join([head, *lines, "", hint]), bulk=True)
            return Final("")

        lines = [head]
        for i, (c, have) in enumerate(cands, 1):
            name = c.title + (f" ({c.year})" if c.year else "")
            if have:
                lines.append(f"{i}. 👍 {name} – schon da")
                continue
            try:
                await self._add(Flow(uuid.uuid4().hex[:10], chat_id, c, monitor="all"))
                lines.append(f"{i}. ✅ {name} – hinzugefügt")
            except Held:
                lines.append(f"{i}. ⏳ {name} – wartet auf Freigabe")
            except ArrError as e:
                lines.append(f"{i}. ❌ {name} – {e}")
        lines.append("\nQualität: Standardprofil. Ich melde mich, wenn Downloads starten.")
        return Final("\n".join(lines))

    async def _library_ids(self, kinds: list[str]) -> dict:
        # tmdb: library + already requested (to skip in suggestions); library: only what Radarr has
        # server: on disk (movie with file, series with at least one episode file)
        owned: dict[str, Any] = {"tmdb": set(), "tvdb": set(), "library": set(), "server": set(), "recent": []}
        recent = []
        if "movie" in kinds and self.radarr:
            for m in await self.radarr.library():
                owned["tmdb"].add(("movie", m.get("tmdbId")))
                owned["library"].add(("movie", m.get("tmdbId")))
                if m.get("hasFile"):
                    owned["server"].add(("movie", m.get("tmdbId")))
                recent.append((m.get("added") or "", "movie", m.get("tmdbId"), m.get("title")))
        if "series" in kinds and self.sonarr:
            for x in await self.sonarr.library():
                owned["tvdb"].add(x.get("tvdbId"))
                if x.get("tmdbId"):
                    owned["tmdb"].add(("series", x["tmdbId"]))
                    if (x.get("statistics") or {}).get("episodeFileCount"):
                        owned["server"].add(("series", x["tmdbId"]))
                    recent.append((x.get("added") or "", "series", x["tmdbId"], x.get("title")))
        for r in self.store.state.requests:
            if r.get("tmdb"):
                owned["tmdb"].add((r["kind"], r["tmdb"]))
        owned["recent"] = [(k, i, t) for _, k, i, t in sorted(recent, key=lambda x: x[0], reverse=True) if i]
        return owned

    async def _t_open_url(self, chat_id: int, url: str) -> dict:
        try:
            info = await urlinfo.identify(url, **self.url_options)
        except urlinfo.UrlError as e:
            raise ArrError(str(e)) from e
        info.pop("description", None)  # not needed to find the title; saves tokens
        return _compact(info)


@dataclass
class Final:
    """Result of an action tool: the turn ends here, without another AI call."""

    text: str


def _year_filter(res: list[dict], year: int | None) -> list[dict]:
    if not year:
        return res
    close = [m for m in res if m.get("year") and abs(m["year"] - year) <= 1]
    return close or res


def _movie_cand(m: dict) -> Candidate:
    return Candidate("movie", m.get("tmdbId"), m.get("title", "?"), m.get("year") or None,
                     lib_id=m.get("id") or None, has_file=bool(m.get("hasFile")))


def _series_cand(s: dict) -> Candidate:
    seasons = sorted(x["seasonNumber"] for x in s.get("seasons", []) if x.get("seasonNumber", 0) > 0)
    stats = s.get("statistics") or {}
    return Candidate("series", s.get("tvdbId"), s.get("title", "?"), s.get("year") or None, seasons,
                     lib_id=s.get("id") or None,
                     has_file=bool(stats.get("episodeCount")) and stats.get("episodeFileCount") == stats.get("episodeCount"))


def _season_buttons(seasons: list[int]) -> list[tuple[str, str]]:
    shown = seasons if len(seasons) <= MAX_SEASON_BUTTONS else seasons[:3]
    buttons = [("Alle Staffeln", "all")] + [(f"Staffel {n}", f"s{n}") for n in shown]
    if len(seasons) > MAX_SEASON_BUTTONS:
        buttons.append((f"Neueste (Staffel {seasons[-1]})", f"s{seasons[-1]}"))
    buttons.append(("Nur neue Folgen", "future"))
    return buttons


def _what(flow: Flow) -> str:
    c = flow.cand
    if c.kind == "series":
        if flow.seasons:
            return f"{c.title} (Staffel {', '.join(map(str, flow.seasons))})"
        if flow.monitor == "future":
            return f"{c.title} (nur neue Folgen)"
    return c.label if c.kind == "movie" else c.title


def queue_title(q: dict) -> str:
    if "movie" in q and q["movie"]:
        return f"{q['movie'].get('title')} ({q['movie'].get('year')})"
    if "series" in q and q["series"]:
        ep = q.get("episode") or {}
        if ep:
            return f"{q['series'].get('title')} S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
        return q["series"].get("title")
    return q.get("title", "?")


def _season_overview(series: dict) -> tuple[list[int], list[int]]:
    """(complete seasons, seasons with missing episodes) – specials (0) ignored."""
    have, missing = [], []
    for x in sorted(series.get("seasons") or [], key=lambda x: x.get("seasonNumber", 0)):
        n = x.get("seasonNumber", 0)
        if n <= 0:
            continue
        st = x.get("statistics") or {}
        files, total = st.get("episodeFileCount", 0), st.get("totalEpisodeCount", 0)
        if total and files >= total:
            have.append(n)
        else:
            missing.append(n)
    return have, missing


def _seasons_text(seasons: list[int], short: bool = False) -> str:
    """[1, 2, 3, 5] -> 'Staffel 1–3, 5'."""
    if not seasons:
        return ""
    parts: list[str] = []
    start = prev = seasons[0]
    for n in seasons[1:] + [None]:  # type: ignore[list-item]
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}–{prev}" if prev - start >= 2 else ", ".join(map(str, range(start, prev + 1))))
        if n is not None:
            start = prev = n
    return ("St. " if short else "Staffel ") + ", ".join(parts)


def release_languages(releases: list[dict]) -> dict:
    """How many indexer releases exist, and how many of them are German."""

    def german(r: dict) -> bool:
        langs = [(lng.get("name") or "").lower() for lng in r.get("languages") or []]
        if isinstance(r.get("language"), dict):  # Sonarr v3
            langs.append((r["language"].get("name") or "").lower())
        title = (r.get("title") or "").lower().replace(" ", ".")
        return "german" in langs or ".german." in f".{title}."

    return {"releases": len(releases), "deutsch": sum(1 for r in releases if german(r))}


def mute(store: Any, chat_id: int, minutes: int | None) -> str:
    """Mute download messages for this chat (minutes) or switch them on again (None)."""
    from datetime import datetime, timezone

    muted = store.state.muted
    key = str(chat_id)
    if minutes is None:
        m = muted.pop(key, None)
        store.save()
        if not m:
            return "🔔 Meldungen waren nicht stummgeschaltet."
        n = m.get("missed", 0)
        return "🔔 Meldungen sind wieder an." + (
            f" In der Zeit gab es {n} Meldung{'en' if n != 1 else ''} – Überblick: /downloads" if n else "")
    minutes = max(1, min(int(minutes), 7 * 24 * 60))
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    old = muted.get(key) or {}
    muted[key] = {"until": until.isoformat(), "missed": old.get("missed", 0), "lines": old.get("lines", [])}
    store.save()
    if minutes < 120:
        span = f"{minutes} Minuten"
    elif minutes < 48 * 60:
        span = f"{minutes / 60:.0f} Stunden"
    else:
        span = f"{minutes / 1440:.0f} Tage"
    return (f"🔕 Stumm für {span}. Download-Meldungen sammle ich und sage dir danach kurz, was war. "
            "Früher wieder an: „Meldungen wieder an“ oder /laut.")


async def resume_search(svc: Any, item_id: int) -> None:
    """Monitor again and start a search right away (used by resume and by the tracker after a pause)."""
    await svc.set_monitored(item_id, True)
    await svc.search(item_id)
