"""🛠 Tools the bot builds for itself ("Rezepte"), only for admins.

When an admin asks something no tool covers ("Top 3 Marvel-Filme"), a stronger model writes a recipe:
a few read-only GET requests against TMDB, Radarr or Sonarr plus filter, sort and a line template.
No code is generated or run - a recipe is plain data, checked against a list of allowed endpoints
here, so it can only read. Adding titles still goes through the normal tools (the recipe can offer
buttons for that). The admin sees the estimated cost before, the real cost after, and keeps or drops it.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .llm import LLMProvider, ToolSpec, Turn

log = logging.getLogger(__name__)

SOURCES = {
    "tmdb": r"^/(discover/(movie|tv)|search/(movie|tv|person|company|collection|keyword)|trending/(movie|tv|all)/(day|week)"
            r"|(movie|tv)/\d+(/(recommendations|similar|credits|keywords|release_dates|watch/providers|season/\d+))?"
            r"|(movie|tv)/(popular|top_rated|upcoming|now_playing|airing_today|on_the_air)"
            r"|person/\d+(/(movie_credits|tv_credits))?|collection/\d+|company/\d+|genre/(movie|tv)/list)$",
    "radarr": r"^/(movie(/\d+)?|calendar|queue|history|wanted/(missing|cutoff)|diskspace|qualityprofile|tag|blocklist)$",
    "sonarr": r"^/(series(/\d+)?|episode|calendar|queue|history|wanted/(missing|cutoff)|diskspace|qualityprofile|tag)$",
}
OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists")
MAX_STEPS = 3
MAX_LINES = 25
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,30}$")


class RecipeError(Exception):
    pass


@dataclass
class Recipe:
    name: str  # snake_case, the tool is called recipe_<name>
    title: str  # German, shown to the admin
    description: str  # when to use it (goes to the AI as tool description)
    params: dict = field(default_factory=dict)  # {name: {"type": "string|integer|number", "description": ...}}
    steps: list = field(default_factory=list)  # lookups: {source, path, query, pick, as}
    source: str = "tmdb"
    path: str = ""
    query: dict = field(default_factory=dict)
    items: str = ""  # dotted path to the list in the answer ("results"; "" = the answer is the list)
    filters: list = field(default_factory=list)  # {field, op, value}
    sort: dict = field(default_factory=dict)  # {field, desc}
    limit: int = 10
    heading: str = ""  # template with params, e.g. "🏆 Top {count} Marvel-Filme"
    line: str = ""  # template per item, e.g. "{title} ({release_date|year}) ⭐ {vote_average|1}"
    offer: str = ""  # movie | series: buttons to add the items
    id_field: str = "id"  # tmdb id of an item (offer)
    created: str = ""
    tokens: int = 0
    cost: float = 0.0
    uses: int = 0

    @property
    def tool(self) -> str:
        return f"recipe_{self.name}"

    def spec(self) -> ToolSpec:
        props = {k: {"type": v.get("type", "string") if v.get("type") in ("string", "integer", "number") else "string",
                     "description": str(v.get("description", ""))[:120]} for k, v in self.params.items()}
        return ToolSpec(self.tool, f"Selbstgebaut: {self.description}"[:300],
                        {"type": "object", "properties": props})


# ---- checking -------------------------------------------------------------------------------------
def _check_request(source: str, path: str, query: Any) -> None:
    if source not in SOURCES:
        raise RecipeError(f"Quelle {source!r} gibt es nicht (erlaubt: {', '.join(SOURCES)}).")
    probe = re.sub(r"\{[a-z_][a-z0-9_]*\}", "1", path or "")  # placeholders in the path stand for ids
    if not re.match(SOURCES[source], probe):
        raise RecipeError(f"{source} {path!r} ist nicht erlaubt (nur lesende Abfragen aus der Liste).")
    if not isinstance(query, dict):
        raise RecipeError("query muss ein Objekt sein.")


def validate(raw: dict) -> Recipe:
    if not isinstance(raw, dict):
        raise RecipeError("Rezept ist kein Objekt.")
    known = set(Recipe.__dataclass_fields__) - {"created", "tokens", "cost", "uses"}
    r = Recipe(**{k: v for k, v in raw.items() if k in known})
    if not NAME_RE.match(r.name or ""):
        raise RecipeError("name: nur a-z, 0-9 und _, 3-31 Zeichen.")
    if not r.title or not r.description or not r.line:
        raise RecipeError("title, description und line sind Pflicht.")
    if not isinstance(r.params, dict) or not isinstance(r.steps, list) or len(r.steps) > MAX_STEPS:
        raise RecipeError(f"params muss ein Objekt sein, höchstens {MAX_STEPS} steps.")
    for s in r.steps:
        if not isinstance(s, dict) or not s.get("as") or not s.get("pick"):
            raise RecipeError("Jeder step braucht source, path, pick und as.")
        _check_request(s.get("source", ""), s.get("path", ""), s.get("query") or {})
    _check_request(r.source, r.path, r.query)
    for f in r.filters:
        if not isinstance(f, dict) or f.get("op") not in OPS or not f.get("field"):
            raise RecipeError(f"Filter braucht field und op ({', '.join(OPS)}).")
    if r.offer not in ("", "movie", "series"):
        raise RecipeError("offer: movie, series oder leer.")
    r.limit = max(1, min(int(r.limit or 10), MAX_LINES))
    return r


# ---- running --------------------------------------------------------------------------------------
def dig(data: Any, path: str) -> Any:
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(data, list):
            data = data[int(part)] if part.lstrip("-").isdigit() and -len(data) <= int(part) < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def _fill(value: Any, env: dict) -> Any:
    """{name} in strings -> value from env. A string that is only one unknown placeholder -> None (dropped)."""
    if isinstance(value, str):
        whole = re.fullmatch(r"\{([a-z_][a-z0-9_]*)\}", value)
        if whole:
            return env.get(whole.group(1))
        return re.sub(r"\{([a-z_][a-z0-9_]*)\}", lambda m: str(env.get(m.group(1), "")), value)
    return value


def _query(q: dict, env: dict) -> dict:
    out = {}
    for k, v in (q or {}).items():
        v = _fill(v, env)
        if v is None or v == "":
            continue
        out[k] = "true" if v is True else "false" if v is False else v
    return out


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _match(item: Any, f: dict, env: dict) -> bool:
    v, want, op = dig(item, f["field"]), _fill(f.get("value"), env), f["op"]
    if op == "exists":
        return v not in (None, "", [], {}) if want in (None, True, "true") else v in (None, "", [], {})
    if op in ("eq", "ne"):
        same = str(v).lower() == str(want).lower()
        return same if op == "eq" else not same
    if op == "contains":
        if isinstance(v, list):
            return any(str(want).lower() in json.dumps(x, ensure_ascii=False).lower() for x in v)
        return str(want).lower() in str(v or "").lower()
    if op == "in":
        options = want if isinstance(want, list) else str(want).split(",")
        return str(v).lower() in [str(o).strip().lower() for o in options]
    a, b = _num(v), _num(want)
    if a is None or b is None:  # dates compare as text
        a, b = str(v or ""), str(want or "")  # type: ignore[assignment]
        if not a:
            return False
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]  # type: ignore[operator]


def render_value(v: Any, fmt: str) -> str:
    if v is None:
        return ""
    if fmt == "year":
        return str(v)[:4]
    if fmt == "date" and isinstance(v, str) and len(v) >= 10:
        return f"{v[8:10]}.{v[5:7]}.{v[:4]}"
    if fmt == "gb":
        n = _num(v)
        return f"{n / 2**30:.1f} GB".replace(".", ",") if n is not None else ""
    if fmt == "count":
        return str(len(v)) if isinstance(v, (list, dict)) else ""
    if fmt.isdigit():
        n = _num(v)
        return f"{n:.{int(fmt)}f}".replace(".", ",") if n is not None else str(v)
    if isinstance(v, list):
        return ", ".join(str(x.get("name") if isinstance(x, dict) else x) for x in v[:3])
    return str(v)


def render_line(template: str, item: Any, env: dict | None = None) -> str:
    def sub(m: re.Match) -> str:
        key, _, fmt = m.group(1).partition("|")
        if env is not None and key in env and dig(item, key) is None:
            return render_value(env[key], fmt)
        return render_value(dig(item, key), fmt)
    text = re.sub(r"\{([a-zA-Z_][\w.]*(?:\|\w+)?)\}", sub, template)
    return re.sub(r"\(\s*\)|⭐\s*(?=$|\s·)", "", text).strip()


async def fetch(clients: dict, source: str, path: str, query: dict) -> Any:
    client = clients.get(source)
    if client is None:
        raise RecipeError(f"{source} ist hier nicht eingerichtet.")
    if not re.match(SOURCES[source], path):  # again with the values filled in
        raise RecipeError(f"{source} {path!r} ist nicht erlaubt.")
    if source == "tmdb":
        return await client._get(path, **query)
    return await client._req("GET", path, params=query)


@dataclass
class Outcome:
    heading: str
    lines: list[str]
    items: list[Any]


async def run(r: Recipe, args: dict, clients: dict) -> Outcome:
    env = {k: v for k, v in (args or {}).items() if k in r.params and v not in (None, "")}
    for s in r.steps:
        data = await fetch(clients, s["source"], _fill(s["path"], env), _query(s.get("query") or {}, env))
        value = dig(data, s["pick"])
        if isinstance(value, list):
            value = "|".join(str(x) for x in value[:5])
        if value in (None, ""):
            raise RecipeError(f"Nichts gefunden für {s['as']}.")
        env[s["as"]] = value
    data = await fetch(clients, r.source, _fill(r.path, env), _query(r.query, env))
    items = dig(data, r.items) if r.items else data
    if not isinstance(items, list):
        items = [items] if items else []
    items = [i for i in items if all(_match(i, f, env) for f in r.filters)]
    if r.sort.get("field"):
        def key(i: Any) -> tuple:
            v = dig(i, r.sort["field"])
            n = _num(v)
            return (v is None, n if n is not None else str(v or ""))
        items = sorted(items, key=key, reverse=bool(r.sort.get("desc")))
        if r.sort.get("desc"):  # missing values last also when descending
            items = [i for i in items if dig(i, r.sort["field"]) is not None] + \
                    [i for i in items if dig(i, r.sort["field"]) is None]
    count = _num(env.get("count"))
    items = items[:min(r.limit, int(count)) if count else r.limit]
    lines = [render_line(r.line, i, env) for i in items]
    heading = render_line(r.heading, {}, {**env, "count": len(items)}) if r.heading else f"🛠 {r.title}"
    return Outcome(heading, [ln for ln in lines if ln], items)


# ---- building -------------------------------------------------------------------------------------
PROMPT = """Du baust ein Werkzeug (Rezept) für einen Telegram-Bot rund um Filme/Serien (Radarr, Sonarr, TMDB).
Ein Rezept ist JSON, kein Code: optionale steps (Vorab-Abfragen, deren Ergebnis per "pick" als Variable "as"
gespeichert wird), dann eine Haupt-Abfrage (source, path, query), aus der Antwort die Liste "items" (Punkt-Pfad),
filters, sort, limit und Textvorlagen. In path, query und filter-Werten ersetzt {name} Parameter oder Variablen.
Eine query-Angabe, die nur aus einem nicht gesetzten Platzhalter besteht, fällt weg (optionale Parameter).
Nur GET. Erlaubte Pfade (Regex):
{sources}
TMDB: API v3 (language=de-DE setzen; discover kennt with_companies, with_genres, with_keywords, with_people,
primary_release_year / first_air_date_year, vote_count.gte, sort_by=vote_count.desc|vote_average.desc|revenue.desc|
primary_release_date.desc). Firmen/Personen/Stichwörter zuerst per search/... in einem step auflösen
(pick z. B. "results.0.id"; eine Liste wird mit | verbunden).
Radarr/Sonarr: API v3, /movie bzw. /series liefert die ganze Bibliothek (Felder wie title, year, sizeOnDisk,
hasFile, monitored, added, ratings.imdb.value, genres, statistics.sizeOnDisk).
Vorlagen: {feld}, {a.b}, Formate {feld|year}, {feld|date}, {feld|1} (Nachkommastellen), {feld|gb}, {feld|count}.
heading darf Parameter und {count} (Anzahl Treffer) nutzen. Ausgabe auf Deutsch, kurz, mit passenden Emojis.
offer=movie|series nur, wenn die Treffer TMDB-Filme/-Serien sind, die man hinzufügen könnte (id_field = TMDB-ID).
Parameter so wählen, dass das Werkzeug auch für ähnliche Fragen taugt (z. B. studio, year, count).
Gib zusätzlich example_args: die Werte für die ursprüngliche Frage."""

SCHEMA = {
    "type": "object",
    "properties": {
        "recipe": {"type": "object", "description": "Das Rezept (Felder: name, title, description, params, steps, "
                   "source, path, query, items, filters, sort, limit, heading, line, offer, id_field)"},
        "example_args": {"type": "object"},
    },
    "required": ["recipe", "example_args"],
}
BUILD = ToolSpec("save_recipe", "Das fertige Rezept abgeben.", SCHEMA)
EST_OUTPUT = 700  # tokens a recipe answer takes, about


def prompt() -> str:
    return PROMPT.replace("{sources}", "\n".join(f"- {k}: {v}" for k, v in SOURCES.items()))


def estimate(request: str) -> tuple[int, int]:
    """(input, output) tokens for one build, about (4 characters per token)."""
    inp = (len(prompt()) + len(json.dumps(SCHEMA)) + len(request)) // 4 + 150
    return inp, EST_OUTPUT


async def build(provider: LLMProvider, request: str, error: str | None = None,
                previous: dict | None = None) -> tuple[Recipe, dict, dict]:
    """One model call -> (recipe, example args, usage). error/previous: second try after a failed check."""
    text = f"Frage des Admins: {request}"
    if error and previous:
        text += (f"\n\nDein letztes Rezept war fehlerhaft: {error}\n"
                 f"{json.dumps(previous, ensure_ascii=False)[:3000]}\nBitte korrigieren.")
    turn = await provider.complete(prompt(), [Turn("user", text=text)], [BUILD])
    call = next((c for c in turn.tool_calls if c.name == "save_recipe"), None)
    if call is None:
        raise RecipeError("Das Modell hat kein Rezept geliefert.")
    raw = call.args.get("recipe") or {}
    try:
        return validate(raw), dict(call.args.get("example_args") or {}), turn.usage or {}
    except (RecipeError, TypeError, ValueError) as e:
        err = RecipeError(str(e))
        err.raw, err.usage = raw, turn.usage or {}  # type: ignore[attr-defined]
        raise err from e


# ---- storage --------------------------------------------------------------------------------------
class RecipeStore:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "profiles.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS recipes (name TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def save(self, r: Recipe) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO recipes VALUES (?, ?)", (r.name, json.dumps(asdict(r), ensure_ascii=False)))

    def all(self) -> list[Recipe]:
        with self._db() as db:
            rows = db.execute("SELECT data FROM recipes ORDER BY name").fetchall()
        out = []
        for (data,) in rows:
            try:
                out.append(Recipe(**json.loads(data)))
            except (TypeError, ValueError) as e:
                log.warning("Rezept unlesbar: %s", e)
        return out

    def get(self, name: str) -> Recipe | None:
        name = name.removeprefix("recipe_")
        return next((r for r in self.all() if r.name == name), None)

    def remove(self, name: str) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM recipes WHERE name = ?", (name.removeprefix("recipe_"),)).rowcount > 0


def render_list(recipes: list[Recipe]) -> str:
    if not recipes:
        return ("🛠 Noch keine selbstgebauten Werkzeuge.\nStell mir eine Frage, für die ich kein Werkzeug habe – "
                "ich biete dann an, mir eins zu bauen.")
    out = [f"🛠 <b>Selbstgebaute Werkzeuge ({len(recipes)})</b>"]
    for r in recipes:
        cost = f" · gebaut für {r.cost * 100:.2f} ct".replace(".", ",") if r.cost else ""
        out.append(f"• <b>{html.escape(r.title)}</b> – {html.escape(r.description)}\n  {r.uses}× benutzt{cost}")
    out.append("\nLöschen: „Lösch das Werkzeug …“")
    return "\n".join(out)
