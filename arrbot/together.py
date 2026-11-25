"""👥 "Was können Anna und ich zusammen schauen?" – suggestions that fit two people.

In code, no AI: both taste profiles score every title, the weaker of the two counts most (a title only
one of you likes is no good evening). Left out: what either of you has watched (Plex) or requested,
turned down, or a genre one of you avoids.
Two pools:
  - already on the server and neither has seen it (Plex): watch tonight
  - new titles: TMDB recommendations seeded by both histories, as buttons to add
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .profiles import Profile, genre_de


@dataclass
class Pick:
    kind: str
    title: str
    year: int | None
    genres: list[str]
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    rating: float = 0.0
    score: float = 0.0
    rating_key: str | None = None  # Plex: already on the server
    because: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


def fit(a: Profile, b: Profile, genres: list[str], rating: float = 0.0) -> float | None:
    """Score for both (None = one of them avoids a genre)."""
    g = [genre_de(x) or x for x in genres]
    if set(g) & (set(a.prefs.avoid) | set(b.prefs.avoid)):
        return None
    ba, bb = a.genre_bonus(g), b.genre_bonus(g)
    return 2 * min(ba, bb) + (ba + bb) / 2 + (rating or 0) / 20


def excluded(a: Profile, b: Profile, kind: str, tmdb_id: int | None) -> bool:
    key = (kind, tmdb_id)
    return bool(tmdb_id) and (key in a.seen or key in b.seen or key in a.rejected or key in b.rejected)


def from_library(items_a: list[Any], items_b: list[Any] | None, a: Profile, b: Profile) -> list[Pick]:
    """Plex items neither of the two has started (items_b None: the other has no Plex - profile only)."""
    seen_b = {(i.kind, i.rating_key) for i in items_b or [] if i.watched or i.started}
    out = []
    for i in items_a:
        if i.watched or i.started or (i.kind, i.rating_key) in seen_b or excluded(a, b, i.kind, i.tmdb):
            continue
        s = fit(a, b, i.genres or [])
        if s is None:
            continue
        out.append(Pick(i.kind, i.title, i.year, [genre_de(x) or x for x in i.genres or []], i.tmdb, i.tvdb,
                        score=s, rating_key=i.rating_key))
    return sorted(out, key=lambda p: -p.score)


def render(name_a: str, name_b: str, here: list[Pick], new_count: int) -> str:
    """Markdown-ish like the other answers (the bot turns ** into bold)."""
    out = [f"👥 **Für {name_a} und {name_b}**"]
    if here:
        out.append("\n**▶️ Schon da, keiner von euch hat es gesehen**")
        out += [f"{'🎬' if p.kind == 'movie' else '📺'} {p.name}" + (f" ({', '.join(p.genres[:2])})" if p.genres else "")
                for p in here]
    if new_count:
        out.append("\n**➕ Neu für euch** – antippen zum Hinzufügen:")
    if not here and not new_count:
        out.append("\nIch finde gerade nichts, was euch beiden passt. Je mehr ihr anfragt und schaut, desto besser wird's.")
    return "\n".join(out)
