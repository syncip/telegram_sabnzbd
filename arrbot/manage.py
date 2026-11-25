"""Series housekeeping without AI tokens: get German episodes, delete episodes that don't fit.

The AI only identifies the series. Checking, picking releases and the confirmation run here;
the bot shows the result with buttons.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .arr import ArrError
from .german import _air, _is_german_release, file_check, file_langs, langs_de

log = logging.getLogger(__name__)

MAX_SEASONS = 6  # indexer searches per run: each takes seconds and costs indexer API hits
PARALLEL = 3
BAD_REJECTIONS = ("unknown", "sample", "blocklist", "wrong", "does not match")


def _ep(e: dict) -> str:
    return f"S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}"


def _eps_text(eps: list[dict], limit: int = 6) -> str:
    labels = [_ep(e) for e in sorted(eps, key=lambda e: (e["seasonNumber"], e["episodeNumber"]))]
    return ", ".join(labels[:limit]) + (f" +{len(labels) - limit}" if len(labels) > limit else "")


def _gb(byte_count: float) -> str:
    gb = byte_count / 2**30
    return (f"{gb:.1f} GB" if gb >= 1 else f"{byte_count / 2**20:.0f} MB").replace(".", ",")


@dataclass
class Grab:
    release: dict
    episodes: list[dict]  # library episodes it covers (only those we need)
    pack: bool = False


@dataclass
class FixPlan:
    series_id: int
    title: str
    checked: int = 0  # aired episodes looked at
    german: list[dict] = field(default_factory=list)  # already German
    missing: list[dict] = field(default_factory=list)  # no file
    other_lang: list[dict] = field(default_factory=list)  # file, but not German
    mislabelled: list[dict] = field(default_factory=list)  # part of other_lang: named German, audio isn't
    grabs: list[Grab] = field(default_factory=list)
    not_found: list[dict] = field(default_factory=list)  # needed, no German download found
    skipped_seasons: list[int] = field(default_factory=list)

    def covered(self, only_missing: bool) -> list[Grab]:
        if not only_missing:
            return self.grabs
        missing_ids = {e["id"] for e in self.missing}
        return [g for g in self.grabs if all(e["id"] in missing_ids for e in g.episodes)]


def _score(r: dict) -> tuple:
    return (bool(r.get("approved")), r.get("customFormatScore") or 0, r.get("qualityWeight") or 0,
            r.get("seeders") or 0, -(r.get("size") or 0))


def _usable(r: dict, bad: set[str] = frozenset()) -> bool:  # type: ignore[assignment]
    """A German release we can grab; never one of the releases that turned out not to be German."""
    if not _is_german_release(r) or not r.get("guid") or r.get("indexerId") is None:
        return False
    if (r.get("title") or "").lower() in bad:
        return False
    rejections = " ".join(r.get("rejections") or []).lower()
    return not any(b in rejections for b in BAD_REJECTIONS)


async def plan_german(sonarr: Any, series_id: int, season: int | None, today: date) -> FixPlan:
    series = await sonarr.get(series_id)
    plan = FixPlan(series_id, series.get("title", "?"))
    eps = [e for e in await sonarr.episodes(series_id, season)
           if e.get("seasonNumber", 0) > 0 and (_air(e) or date.max) <= today]
    plan.checked = len(eps)
    bad: set[str] = set()  # release names of files that are not really German
    for e in eps:
        check = file_check(e.get("episodeFile")) if e.get("hasFile") else None
        if check == "german":
            plan.german.append(e)
        elif e.get("hasFile"):
            plan.other_lang.append(e)
            if check == "mislabelled":
                plan.mislabelled.append(e)
            scene = (e.get("episodeFile") or {}).get("sceneName")
            if scene:
                bad.add(scene.lower())
        else:
            plan.missing.append(e)

    needed = plan.missing + plan.other_lang
    seasons = sorted({e["seasonNumber"] for e in needed})
    plan.skipped_seasons = seasons[MAX_SEASONS:]
    seasons = seasons[:MAX_SEASONS]
    sem = asyncio.Semaphore(PARALLEL)

    async def search(n: int) -> tuple[int, list[dict] | Exception]:
        async with sem:
            try:
                return n, await sonarr.releases(seriesId=series_id, seasonNumber=n)
            except ArrError as e:
                return n, e

    for n, releases in await asyncio.gather(*(search(n) for n in seasons)):
        season_needed = {e["episodeNumber"]: e for e in needed if e["seasonNumber"] == n}
        if isinstance(releases, Exception):
            log.warning("Release-Suche Staffel %s: %s", n, releases)
            plan.not_found += season_needed.values()
            continue
        german = [r for r in releases if _usable(r, bad)]
        packs = sorted((r for r in german if r.get("fullSeason")), key=_score, reverse=True)
        singles: dict[int, dict] = {}
        for r in german:
            if r.get("fullSeason"):
                continue
            for num in r.get("mappedEpisodeNumbers") or r.get("episodeNumbers") or []:
                if num in season_needed and (num not in singles or _score(r) > _score(singles[num])):
                    singles[num] = r
        uncovered = set(season_needed) - set(singles)
        # A season pack is one download instead of many, and fills gaps single releases can't.
        if packs and (uncovered or len(season_needed) >= 3):
            plan.grabs.append(Grab(packs[0], list(season_needed.values()), pack=True))
            continue
        by_release: dict[str, Grab] = {}
        for num, r in singles.items():
            g = by_release.setdefault(r["guid"], Grab(r, []))
            g.episodes.append(season_needed[num])
        plan.grabs += by_release.values()
        plan.not_found += [season_needed[num] for num in sorted(uncovered)]
    for n in plan.skipped_seasons:
        plan.not_found += [e for e in needed if e["seasonNumber"] == n]
    return plan


def render_plan(plan: FixPlan) -> tuple[str, list[tuple[str, str]]]:
    lines = [f"🇩🇪 {plan.title}: {plan.checked} ausgestrahlte Folgen geprüft"]
    lines.append(f"✅ {len(plan.german)} auf Deutsch")
    if plan.other_lang:
        lines.append(f"📁 {len(plan.other_lang)} nur in anderer Sprache ({_eps_text(plan.other_lang)})")
    if plan.mislabelled:
        lines.append(f"🏷 Davon {len(plan.mislabelled)} als Deutsch benannt, die Tonspur ist aber "
                     f"{langs_de(file_langs(plan.mislabelled[0].get('episodeFile')))} ({_eps_text(plan.mislabelled)})")
    if plan.missing:
        lines.append(f"❌ {len(plan.missing)} fehlen ({_eps_text(plan.missing)})")
    if not plan.other_lang and not plan.missing:
        lines.append("\n👍 Alles ist auf Deutsch da.")
        return "\n".join(lines), []

    covered = [e for g in plan.grabs for e in g.episodes]
    if covered:
        packs = sum(1 for g in plan.grabs if g.pack)
        how = f" ({packs} Staffelpaket{'e' if packs != 1 else ''}, {len(plan.grabs) - packs} einzeln)" if packs else ""
        lines.append(f"\n⬇️ Deutsche Downloads gefunden für {len(covered)} Folgen{how}")
    if plan.not_found:
        lines.append(f"🔍 Noch nichts Deutsches für: {_eps_text(plan.not_found)}")
    if plan.skipped_seasons:
        lines.append(f"ℹ️ Staffel {', '.join(map(str, plan.skipped_seasons))} nicht durchsucht (max. {MAX_SEASONS} "
                     "auf einmal) – frag einfach nochmal mit Staffel.")
    if not covered:
        return "\n".join(lines), []

    replace = [e for g in plan.grabs for e in g.episodes if e.get("hasFile")]
    buttons = []
    if replace:
        lines.append(f"\nDabei werden {len(replace)} Dateien in anderer Sprache durch die deutschen ersetzt.")
        buttons.append((f"⬇️ Alle {len(covered)} auf Deutsch laden", "all"))
        only_missing = [e for g in plan.covered(only_missing=True) for e in g.episodes]
        if only_missing:
            buttons.append((f"⬇️ Nur fehlende ({len(only_missing)})", "missing"))
    else:
        buttons.append((f"⬇️ {len(covered)} Folgen auf Deutsch laden", "all"))
    buttons.append(("Abbrechen", "no"))
    return "\n".join(lines), buttons


async def run_fix(sonarr: Any, plan: FixPlan, only_missing: bool) -> str:
    grabs = plan.covered(only_missing)
    if not grabs:
        return "Nichts zu tun."
    # Existing files in other languages go first, otherwise Sonarr refuses the German one as "no upgrade".
    old_files = sorted({e["episodeFileId"] for g in grabs for e in g.episodes if e.get("hasFile") and e.get("episodeFileId")})
    await sonarr.set_episodes_monitored(sorted({e["id"] for g in grabs for e in g.episodes}), True)
    started, failed = 0, []
    for g in grabs:
        try:
            await sonarr.grab(g.release)
            started += 1
        except ArrError as e:
            failed.append(f"{_eps_text(g.episodes)}: {e}")
    if started and old_files:
        await sonarr.delete_episode_files(old_files)
    eps = sum(len(g.episodes) for g in grabs)
    out = [f"⬇️ {started} deutsche Download{'s' if started != 1 else ''} gestartet für {eps} Folgen von {plan.title}."]
    if old_files and started:
        out.append(f"🗑 {len(old_files)} Dateien in anderer Sprache entfernt.")
    if failed:
        out.append("❌ Nicht geklappt: " + "; ".join(failed[:3]))
    out.append("Ich melde mich, wenn sie fertig sind.")
    return "\n".join(out)


# ---- deleting episodes ------------------------------------------------------------------------
@dataclass
class CleanPlan:
    series_id: int
    title: str
    what: str
    episodes: list[dict]

    @property
    def size(self) -> float:
        return sum((e.get("episodeFile") or {}).get("size") or 0 for e in self.episodes)


async def plan_clean(sonarr: Any, series_id: int, which: str, season: int | None) -> CleanPlan:
    series = await sonarr.get(series_id)
    eps = [e for e in await sonarr.episodes(series_id, season) if e.get("hasFile") and e.get("episodeFileId")]
    if which == "not_german":
        eps = [e for e in eps if file_check(e.get("episodeFile")) != "german"]
        what = "nicht auf Deutsch"
    elif season is not None:
        what = f"aus Staffel {season}"
    else:
        what = "alle"
    return CleanPlan(series_id, series.get("title", "?"), what, eps)


def render_clean(plan: CleanPlan) -> tuple[str, list[tuple[str, str]]]:
    if not plan.episodes:
        return f"👍 {plan.title}: keine Folgen {plan.what} gefunden – nichts zu löschen.", []
    text = (f"🗑 {plan.title}: {len(plan.episodes)} Folgen {plan.what} ({_gb(plan.size)})\n"
            f"{_eps_text(plan.episodes, 10)}\n\nWas soll passieren?")
    buttons = [("🗑 Löschen & auf Deutsch neu suchen", "research"), ("🗑 Nur löschen, nicht neu laden", "drop"),
               ("Abbrechen", "no")]
    return text, buttons


async def run_clean(sonarr: Any, plan: CleanPlan, research: bool) -> str:
    ids = [e["id"] for e in plan.episodes]
    await sonarr.delete_episode_files(sorted({e["episodeFileId"] for e in plan.episodes}))
    if research:
        await sonarr.set_episodes_monitored(ids, True)
        await sonarr.search_episodes(ids)
        tail = "Sonarr sucht sie jetzt neu – mit deutschem Qualitätsprofil kommen sie auf Deutsch."
    else:
        await sonarr.set_episodes_monitored(ids, False)  # otherwise Sonarr would download them again
        tail = "Sie werden nicht mehr automatisch geladen."
    return f"🗑 {len(plan.episodes)} Folgen von {plan.title} gelöscht ({_gb(plan.size)} frei). {tail}"


# ---- movies -------------------------------------------------------------------------------------
@dataclass
class MovieFix:
    movie_id: int
    title: str
    check: str  # german | mislabelled | other | unknown | missing
    langs: list[str]
    file_id: int | None
    release: dict | None = None
    releases: int = 0


async def plan_movie(radarr: Any, movie_id: int) -> MovieFix:
    movie = await radarr.get(movie_id)
    title = f"{movie.get('title')} ({movie.get('year')})" if movie.get("year") else movie.get("title", "?")
    mfile = movie.get("movieFile") if movie.get("hasFile") else None
    fix = MovieFix(movie_id, title, file_check(mfile) if mfile else "missing", file_langs(mfile) if mfile else [],
                   (mfile or {}).get("id"))
    if fix.check == "german":
        return fix
    bad = {((mfile or {}).get("sceneName") or "").lower()} - {""}
    rel = await radarr.releases(movieId=movie_id)
    fix.releases = len(rel)
    ok = sorted((r for r in rel if _usable(r, bad)), key=_score, reverse=True)
    fix.release = ok[0] if ok else None
    return fix


def render_movie(fix: MovieFix) -> tuple[str, list[tuple[str, str]]]:
    if fix.check == "german":
        return f"✅ {fix.title}: Die Tonspur ist Deutsch ({langs_de(fix.langs)}). Alles gut.", []
    if fix.check == "mislabelled":
        head = f"🏷 {fix.title}: Die Datei heißt Deutsch, die Tonspur ist aber {langs_de(fix.langs)}."
    elif fix.check == "missing":
        head = f"⏳ {fix.title} ist noch nicht geladen."
    else:
        head = f"📁 {fix.title} ist bei dir nur auf {langs_de(fix.langs)}."
    if not fix.release:
        return head + f"\n🔍 Auf deinen Indexern gibt es noch kein (anderes) deutsches Release ({fix.releases} geprüft).", []
    size = _gb(fix.release.get("size") or 0)
    text = f"{head}\n\n⬇️ Deutsches Release gefunden: {fix.release.get('title')} ({size})"
    label = "🔁 Datei ersetzen" if fix.file_id else "⬇️ Laden"
    return text, [(label, "all"), ("Abbrechen", "no")]


async def run_movie(radarr: Any, fix: MovieFix) -> str:
    await radarr.grab(fix.release)
    if fix.file_id:  # otherwise Radarr keeps the old file as "no upgrade"
        await radarr.delete_movie_file(fix.file_id)
    return (f"⬇️ Deutsche Fassung von {fix.title} wird geladen" + (", die alte Datei ist gelöscht" if fix.file_id else "")
            + ". Ich melde mich, wenn sie fertig ist.")
