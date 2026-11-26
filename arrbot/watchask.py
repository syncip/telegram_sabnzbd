"""👁 Watch check: the admin saw a movie or seasons of a series in Plex, so now and then the bot asks
whether it can go.

Once a day (and only every EVERY days, at most MAX questions) the bot looks at the admin's Plex history:
  - movies the admin watched completely, the last view at least AFTER days ago (not while still on it)
  - series: seasons the admin saw completely (or the whole series), the last view at least AFTER days ago
Not asked: titles marked "🛡 behalten", titles someone else is watching or requested lately (same rules
as the media manager), and anything already answered: "behalten" means never again for that item,
"später" means again in LATER_DAYS days.
Deleting goes through Radarr/Sonarr like the media manager: no import exclusion, it can come back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import mediamgr
from .media import Report, Title, size_text

EVERY = 3  # days between rounds of questions
MAX = 3  # questions per round
AFTER = 2  # days since the last view
LATER_DAYS = 14


@dataclass
class Settings:
    on: bool = True
    every: int = EVERY
    max: int = MAX
    after: int = AFTER

    def text(self) -> str:
        if not self.on:
            return "👁 Nachfragen zu gesehenen Titeln: aus."
        return (f"👁 Nachfragen zu gesehenen Titeln: an – alle {self.every} Tage höchstens {self.max}, "
                f"frühestens {self.after} Tage nach dem Anschauen.")


def load(store: Any) -> Settings:
    s = Settings()
    if store is None:
        return s
    s.on = store.setting("wa:on") != "0"
    for key in ("every", "max", "after"):
        raw = store.setting(f"wa:{key}")
        if raw and raw.isdigit():
            setattr(s, key, int(raw))
    return s


def save(store: Any, s: Settings) -> None:
    store.set_setting("wa:on", "1" if s.on else "0")
    for key in ("every", "max", "after"):
        store.set_setting(f"wa:{key}", str(getattr(s, key)))


def due(store: Any, s: Settings, now: datetime | None = None) -> bool:
    if not s.on:
        return False
    now = now or datetime.now(timezone.utc)
    last = store.setting("wa:last")
    return not last or datetime.fromisoformat(last) <= now - timedelta(days=s.every) + timedelta(hours=1)


def mark_round(store: Any, now: datetime | None = None) -> None:
    store.set_setting("wa:last", (now or datetime.now(timezone.utc)).isoformat())


@dataclass
class Ask:
    """One question: a movie, or a series with the seasons the admin saw."""

    title: Title
    items: list[mediamgr.Item] = field(default_factory=list)  # movie, whole series, or seen seasons
    whole: mediamgr.Item | None = None  # series only: the whole series (when all of it was seen)

    @property
    def key(self) -> str:
        if self.title.kind == "movie":
            return f"radarr:{self.title.arr_id}"
        seasons = ",".join(i.name.split("Staffel ")[-1].split(" ")[0] for i in self.items if i.what == "season")
        return f"sonarr:{self.title.arr_id}:" + (seasons or "all")


def _answered(store: Any, key: str, now: datetime) -> bool:
    raw = store.setting(f"wa:a:{key}")
    if not raw:
        return False
    answer, _, at = raw.partition("|")
    if answer == "later":
        try:
            return datetime.fromisoformat(at) > now - timedelta(days=LATER_DAYS)
        except ValueError:
            return False
    return True


def answer(store: Any, key: str, value: str, now: datetime | None = None) -> None:
    store.set_setting(f"wa:a:{key}", f"{value}|{(now or datetime.now(timezone.utc)).isoformat()}")


def candidates(report: Report, admin_id: int, kept: set[tuple[str, int]], recent: dict[tuple[str, int], int],
               after: int, now: datetime | None = None) -> tuple[list[Title], list[Title]]:
    """Movies and series worth a question (most recently watched first), before the per-season check."""
    now = now or datetime.now(timezone.utc)
    movies, _ = mediamgr.select(report, mediamgr.Query("movie", watched=True, seen_days=after), admin_id, kept,
                                recent, now=now)
    series, _ = mediamgr.select(report, mediamgr.Query("series", watched=True, scope="seasons"), admin_id, kept,
                                recent, now=now)
    cutoff = now - timedelta(days=after)
    series = [t for t in series if (seen := t.viewed_by.get(admin_id)) and seen <= cutoff]
    by_view = lambda t: t.viewed_by.get(admin_id) or now  # noqa: E731
    return sorted(movies, key=by_view, reverse=True), sorted(series, key=by_view, reverse=True)


def series_ask(t: Title, episodes: list[dict], leaves: list[dict], admin_id: int,
               now: datetime | None = None) -> Ask | None:
    seasons = mediamgr.plan_parts(t, episodes, leaves, mediamgr.Query("series", watched=True, scope="seasons"),
                                  admin_id, now)
    seasons = [i for i in seasons if "Staffel " in i.name.rsplit(" – ", 1)[-1]]  # no Specials
    if not seasons:
        return None
    with_file = {e.get("seasonNumber") or 0 for e in episodes if e.get("hasFile")}
    seen = {int(i.name.split("Staffel ")[-1].split(" ")[0]) for i in seasons if "Staffel " in i.name}
    all_seen = admin_id in t.complete_by or not (with_file - {0}) - seen
    return Ask(t, seasons, mediamgr.whole(t) if all_seen and t.ended else None)


def question(a: Ask) -> tuple[str, list[tuple[str, str]]]:
    t = a.title
    if t.kind == "movie":
        text = (f"👁 Du hast {t.name} gesehen ({size_text(t.size)}).\nSoll ich ihn löschen? "
                "Gesperrt wird nichts – du kannst ihn jederzeit wieder holen.")
        buttons = [("🗑 Löschen", "del")]
    else:
        names = ", ".join(i.name.split(" – ")[-1].split(" (")[0] for i in a.items)
        size = sum(i.size for i in a.items)
        text = f"👁 Von {t.name} hast du {names} gesehen ({size_text(size)}).\nSoll das weg?"
        buttons = [(f"🗑 Gesehene Staffeln löschen ({size_text(size)})", "del")]
        if a.whole is not None:
            buttons.append((f"🗑 Ganze Serie löschen ({size_text(a.whole.size)})", "whole"))
        text += " Gesperrt wird nichts – such nochmal holt es zurück."
    buttons += [("➡️ Behalten", "no"), ("⏰ Später fragen", "later"), ("🛡 Nie löschen", "keep")]
    return text, buttons
