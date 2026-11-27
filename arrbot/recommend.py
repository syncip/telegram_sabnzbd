"""💡 Recommendation logic in code (no AI tokens): moods, variety, hidden gems, German availability.

The candidates come from TMDB ("people who liked X also liked …") for starting points that say most
about a person: liked (👍, ⭐4–5 in Plex/bot), watched to the end in Plex, then requested titles.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

from .profiles import TMDB_GENRES

# Words in a wish -> German genre names (TMDB). Several words may point to the same genres.
MOODS: dict[str, list[str]] = {
    "lustig": ["Komödie"], "witzig": ["Komödie"], "komödie": ["Komödie"], "comedy": ["Komödie"],
    "zum lachen": ["Komödie"], "leicht": ["Komödie", "Familie", "Animation"],
    "entspannt": ["Komödie", "Familie", "Animation"], "gute laune": ["Komödie", "Musik", "Familie"],
    "spannend": ["Thriller", "Krimi", "Action", "Mystery"], "nervenkitzel": ["Thriller", "Horror"],
    "krimi": ["Krimi"], "thriller": ["Thriller"], "action": ["Action"], "explosion": ["Action"],
    "gruselig": ["Horror"], "horror": ["Horror"], "schaurig": ["Horror", "Mystery"],
    "romantisch": ["Liebesfilm"], "liebe": ["Liebesfilm"], "date": ["Liebesfilm", "Komödie"],
    "nachdenklich": ["Drama"], "tiefgründig": ["Drama"], "traurig": ["Drama"], "drama": ["Drama"],
    "episch": ["Abenteuer", "Fantasy", "Science Fiction"], "abenteuer": ["Abenteuer"],
    "fantasy": ["Fantasy"], "sci-fi": ["Science Fiction"], "science fiction": ["Science Fiction"],
    "weltraum": ["Science Fiction"], "familie": ["Familie", "Animation"], "kinder": ["Familie", "Animation", "Kinder"],
    "animiert": ["Animation"], "zeichentrick": ["Animation"], "doku": ["Dokumentarfilm"],
    "dokumentation": ["Dokumentarfilm"], "wahre geschichte": ["Historie", "Drama"], "historisch": ["Historie"],
    "krieg": ["Kriegsfilm"], "western": ["Western"], "mystery": ["Mystery"], "rätsel": ["Mystery"],
    "musik": ["Musik"],
}


@dataclass(frozen=True)
class Situation:
    """Where/how someone watches ("beim Sport", "zum Einschlafen"): genres that fit, genres that don't, and
    how long a film or an episode may be. A title needs one of `prefer`. Of `avoid`, the mild ones (Drama,
    Krimi, Mystery) are fine next to a `prefer` genre (a comedy with some drama works "nebenbei"); the hard
    ones (Horror, Thriller …) never."""

    key: str
    label: str
    intro: str
    prefer: frozenset[str]
    avoid: frozenset[str]
    max_minutes: tuple[tuple[str, int], ...] = ()

    def minutes(self, kind: str) -> int | None:
        return dict(self.max_minutes).get(kind)

    def fits(self, genres: list[str]) -> bool:
        g = set(genres)
        return bool(g & self.prefer) and not g & (self.avoid - MILD)


MILD = frozenset({"Drama", "Krimi", "Mystery"})
SITUATIONS = {
    "nebenbei": Situation(
        "nebenbei", "nebenbei", "🎯 Für nebenbei – leicht zu folgen, kurze Folgen",
        frozenset({"Komödie", "Animation", "Reality", "Dokumentarfilm", "Talkshow", "Familie"}),
        frozenset({"Horror", "Thriller", "Mystery", "Krimi", "Drama", "Kriegsfilm", "Seifenoper"}),
        (("series", 35), ("movie", 110))),
    "einschlafen": Situation(
        "einschlafen", "zum Einschlafen", "🌙 Zum Einschlafen – ruhig, nichts Aufregendes",
        frozenset({"Dokumentarfilm", "Animation", "Familie", "Komödie"}),
        frozenset({"Horror", "Thriller", "Action", "Krimi", "Mystery", "Kriegsfilm", "Drama"}),
        (("series", 30), ("movie", 100))),
    "kinder": Situation(
        "kinder", "mit Kindern", "👨‍👩‍👧 Mit Kindern",
        frozenset({"Familie", "Animation", "Kinder"}),
        frozenset({"Horror", "Thriller", "Krimi", "Kriegsfilm"})),
}
SITUATION_WORDS = [
    ("einschlafen", "einschlafen"), ("zum schlafen", "einschlafen"), ("im bett", "einschlafen"),
    ("mit kind", "kinder"), ("mit den kids", "kinder"), ("mit meinen kids", "kinder"), ("für kinder", "kinder"),
    ("nebenbei", "nebenbei"), ("nebenher", "nebenbei"), ("sport", "nebenbei"), ("fitness", "nebenbei"),
    ("cardio", "nebenbei"), ("laufband", "nebenbei"), ("training", "nebenbei"), ("trainier", "nebenbei"),
    ("auf dem rad", "nebenbei"), ("ergometer", "nebenbei"), ("crosstrainer", "nebenbei"), ("stepper", "nebenbei"),
    ("bügeln", "nebenbei"), ("kochen", "nebenbei"), ("putzen", "nebenbei"), ("aufräumen", "nebenbei"),
    ("hausarbeit", "nebenbei"), ("pendeln", "nebenbei"), ("in der bahn", "nebenbei"), ("im zug", "nebenbei"),
    ("mittagspause", "nebenbei"), ("kurze pause", "nebenbei"), ("beim essen", "nebenbei"),
    ("hintergrund", "nebenbei"), ("berieseln", "nebenbei"), ("abschalten", "nebenbei"), ("hirn aus", "nebenbei"),
]


def situation_of(*texts: str | None) -> Situation | None:
    low = " ".join(t for t in texts if t).lower()
    if not low:
        return None
    if low.strip() in SITUATIONS:
        return SITUATIONS[low.strip()]
    for word, key in SITUATION_WORDS:
        if word in low:
            return SITUATIONS[key]
    return None


GENRE_IDS: dict[str, dict[str, list[int]]] = {"movie": {}, "series": {}}
_TV_ONLY = {10759, 10762, 10763, 10764, 10765, 10766, 10767, 10768}
_MOVIE_ONLY = {28, 12, 14, 27, 10402, 10749, 878, 10770, 53, 10752}
for _gid, _name in TMDB_GENRES.items():
    if _gid not in _TV_ONLY:
        GENRE_IDS["movie"].setdefault(_name, []).append(_gid)
    if _gid not in _MOVIE_ONLY:
        GENRE_IDS["series"].setdefault(_name, []).append(_gid)


def wanted_genres(mood: str | None, genres: list[str] | None) -> set[str]:
    """German genre names from a mood text ("was Lustiges, Spannendes") and named genres."""
    out: set[str] = set()
    low = (mood or "").lower()
    for word, names in MOODS.items():
        if word in low:
            out.update(names)
    known = {n.lower(): n for n in TMDB_GENRES.values()}
    for g in genres or []:
        g = g.strip().lower()
        if g in known:
            out.add(known[g])
        elif g in MOODS:
            out.update(MOODS[g])
    return out


def genre_ids(kind: str, names: set[str]) -> list[int]:
    return sorted({i for n in names for i in GENRE_IDS[kind].get(n, [])})


def franchise(title: str) -> str:
    """'Harry Potter und der Stein der Weisen' and '… und die Kammer des Schreckens' -> same key."""
    t = re.split(r"[:\-–(]", title.lower())[0]
    t = re.sub(r"\b(\d+|ii|iii|iv|teil|part|the|der|die|das)\b", " ", t)
    words = re.findall(r"[a-zäöüß0-9]+", t)
    return " ".join(words[:2])


def score(r: Any, bonus: float = 0.0) -> float:
    """Fit (how many starting points led here) + quality (rating, weighted by how many voted) + taste."""
    return r.score + r.rating / 10 * min(1.0, math.log10(r.votes + 1) / 3) + bonus


def is_gem(r: Any) -> bool:
    """Well rated, but few people know it."""
    return r.rating >= 7.2 and 80 <= r.votes <= 1500


def diversify(ranked: list[Any], n: int, collections: dict[tuple[str, int], int] | None = None) -> list[Any]:
    """Best first, but not three parts of one series of films and not only one genre.
    One hidden gem gets a place if there is one."""
    collections = collections or {}
    picked: list[Any] = []
    seen_fr: set = set()
    genre_count: dict[str, int] = {}
    cap = max(2, math.ceil(n / 2))

    def ok(r: Any) -> bool:
        key = collections.get((r.kind, r.tmdb_id)) or franchise(r.title)
        if key in seen_fr:
            return False
        main = r.genres[0] if r.genres else ""
        return not main or genre_count.get(main, 0) < cap

    def take(r: Any) -> None:
        picked.append(r)
        seen_fr.add(collections.get((r.kind, r.tmdb_id)) or franchise(r.title))
        if r.genres:
            genre_count[r.genres[0]] = genre_count.get(r.genres[0], 0) + 1

    for r in ranked:
        if len(picked) >= n - 1:
            break
        if ok(r):
            take(r)
    gem = next((r for r in ranked if r not in picked and is_gem(r) and ok(r)), None)
    if gem is not None:
        gem.because = ["💎 Geheimtipp"] + [b for b in gem.because if b != "💎 Geheimtipp"]
        take(gem)
    for r in ranked:  # fill up (e.g. everything was one genre)
        if len(picked) >= n:
            break
        if r not in picked and ok(r):
            take(r)
    for r in ranked:
        if len(picked) >= n:
            break
        if r not in picked:
            picked.append(r)
    return picked[:n]


def german_available(info: Any) -> bool:
    """A German version exists (or the original is German): released/streaming in Germany."""
    return bool(info.german_original or info.cinema or info.digital or info.disc or info.tv
                or info.streaming or info.buy_rent)


def reason(r: Any) -> str:
    gem = "💎 Geheimtipp" in r.because
    seeds = [b for b in r.because if b != "💎 Geheimtipp"]
    out = "💎 Geheimtipp" if gem else ""
    if seeds:
        out += (" – " if out else "") + "wie " + ", ".join(seeds[:2])
    return out
