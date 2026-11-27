"""🧬 Duplicate finder (admin): the same film or episode more than once in Plex. The best version stays.

What counts as "the same" is decided by the content Plex measured, not by file names or sizes:
  - Plex itself put several versions under one title (it matched them to the same film/episode), or two
    titles point to the same film (TMDB/IMDb id) or the same episode of a show (season + number);
  - and the running times are alike: similarity = shorter / longer running time. At or above the
    threshold (default 97 %) it is a duplicate. Somewhat below (down to threshold - 10) it might be
    another cut (Extended, Director's Cut): the bot only offers it one by one and asks. Further apart:
    different content, left alone.
Which version stays: preferred audio language first (default German, then English), then the preferred
resolution (default the highest), then the higher bitrate. Both can be changed by the admin.

Deleting goes through Plex (the server setting "Allow media deletion" must be on). Radarr/Sonarr rescan
the title afterwards, so they notice which file is left.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .media import size_text

DEFAULT_PREFS = {"languages": ["de", "en"], "resolutions": ["2160", "1080", "720", "576", "480", "sd"],
                 "language_first": True, "similarity": 97}
LANG = {"ger": "de", "deu": "de", "de": "de", "german": "de", "deutsch": "de", "eng": "en", "en": "en",
        "english": "en", "englisch": "en", "fre": "fr", "fra": "fr", "fr": "fr", "spa": "es", "es": "es",
        "ita": "it", "it": "it", "jpn": "ja", "ja": "ja"}
LANG_NAME = {"de": "Deutsch", "en": "Englisch", "fr": "Französisch", "es": "Spanisch", "it": "Italienisch",
             "ja": "Japanisch"}
UNSURE_SPAN = 10  # percent below the threshold that still get asked about
PREVIEW_LINES = 15
PAGE = 8  # duplicates per page of the full list (with file names: Telegram allows 4096 characters)


def load_prefs(raw: str | None) -> dict:
    out = dict(DEFAULT_PREFS)
    if raw:
        try:
            out.update(json.loads(raw))
        except ValueError:
            pass
    return out


def norm_resolution(r: Any) -> str:
    s = str(r or "").lower().rstrip("p")
    return {"4k": "2160", "uhd": "2160", "fhd": "1080", "hd": "720"}.get(s, s or "sd")


def norm_language(x: Any) -> str | None:
    s = str(x or "").strip().lower()
    return LANG.get(s) or LANG.get(s[:3]) or (s[:2] if len(s) == 2 else None)


@dataclass
class Version:
    rating_key: str  # the Plex title it belongs to
    media_id: int
    title: str
    file: str
    size: int
    duration: int  # ms
    resolution: str
    bitrate: int
    languages: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        res = "4K" if self.resolution == "2160" else (f"{self.resolution}p" if self.resolution.isdigit() else "SD")
        langs = "/".join(LANG_NAME.get(x, x) for x in self.languages) or "Sprache ?"
        return f"{res} · {langs} · {self.duration // 60000} Min. · {size_text(self.size)}"


@dataclass
class Dupe:
    """One version to delete because a better one of the same content stays."""

    name: str  # "Dune (2021)" / "Dark S01E03"
    keep: Version
    drop: Version
    similarity: float  # percent
    sure: bool
    ext: tuple[str, int | str] | None = None  # ("movie", tmdb) / ("series", show title) for the rescan


@dataclass
class DupeJob:
    items: list[Dupe] = field(default_factory=list)
    other_cuts: int = 0  # same title, but clearly different running time: left alone
    pos: int = 0
    deleted: list[Dupe] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: int = 0
    page: int = 0
    no_details: int = 0  # titles whose audio languages were not fetched (very big libraries)


def similarity(a: Version, b: Version) -> float:
    if not a.duration or not b.duration:
        return 0.0
    return 100.0 * min(a.duration, b.duration) / max(a.duration, b.duration)


def rank(v: Version, prefs: dict) -> tuple:
    langs = prefs.get("languages") or []
    lang = min((langs.index(x) for x in v.languages if x in langs), default=len(langs))
    resolutions = prefs.get("resolutions") or DEFAULT_PREFS["resolutions"]
    res = resolutions.index(v.resolution) if v.resolution in resolutions else len(resolutions)
    first = (lang, res) if prefs.get("language_first", True) else (res, lang)
    return (*first, -v.bitrate, -v.size)


def versions_of(meta: dict) -> list[Version]:
    out = []
    for m in meta.get("Media") or []:
        parts = m.get("Part") or []
        langs: list[str] = []
        for p in parts:
            for st in p.get("Stream") or []:
                if st.get("streamType") == 2:
                    code = norm_language(st.get("languageCode") or st.get("language"))
                    if code and code not in langs:
                        langs.append(code)
        out.append(Version(str(meta.get("ratingKey")), int(m.get("id") or 0), meta.get("title") or "?",
                           (parts[0].get("file") or "") if parts else "", sum(int(p.get("size") or 0) for p in parts),
                           int(m.get("duration") or meta.get("duration") or 0), norm_resolution(m.get("videoResolution")),
                           int(m.get("bitrate") or 0), langs))
    return out


def _guid(meta: dict) -> str | None:
    for g in meta.get("Guid") or []:
        gid = str(g.get("id") or "")
        if gid.startswith(("tmdb://", "imdb://")):
            return gid
    return None


def _key(meta: dict) -> tuple | None:
    if meta.get("type") == "episode":
        show = meta.get("grandparentRatingKey") or meta.get("grandparentTitle")
        if show is None or meta.get("index") is None:
            return None
        return ("ep", str(show), int(meta.get("parentIndex") or 0), int(meta["index"]))
    g = _guid(meta)
    return ("movie", g) if g else None


def name_of(meta: dict) -> str:
    if meta.get("type") == "episode":
        return f"{meta.get('grandparentTitle', '?')} S{int(meta.get('parentIndex') or 0):02d}E{int(meta.get('index') or 0):02d}"
    return f"{meta.get('title', '?')} ({meta['year']})" if meta.get("year") else meta.get("title", "?")


def groups(metas: list[dict]) -> list[list[dict]]:
    """Titles that are the same content: one title with several versions, or several titles with one id."""
    by: dict[tuple, list[dict]] = {}
    alone: list[list[dict]] = []
    for m in metas:
        k = _key(m)
        if k is None:
            if len(m.get("Media") or []) > 1:
                alone.append([m])
            continue
        by.setdefault(k, []).append(m)
    out = [g for g in by.values() if sum(len(x.get("Media") or []) for x in g) > 1]
    return out + alone


def find(metas: list[dict], prefs: dict) -> DupeJob:
    """metas: Plex titles with their Media (and audio streams for the groups that matter)."""
    threshold = float(prefs.get("similarity") or DEFAULT_PREFS["similarity"])
    job = DupeJob()
    for g in groups(metas):
        vs = [v for m in g for v in versions_of(m)]
        if len(vs) < 2:
            continue
        best = min(vs, key=lambda v: rank(v, prefs))
        name = name_of(g[0])
        ext: tuple | None = None
        if g[0].get("type") == "episode":
            ext = ("series", g[0].get("grandparentTitle") or "")
        else:
            tm = next((str(x.get("id"))[7:] for x in g[0].get("Guid") or [] if str(x.get("id", "")).startswith("tmdb://")), None)
            ext = ("movie", int(tm)) if tm and tm.isdigit() else None
        for v in vs:
            if v is best:
                continue
            sim = similarity(best, v)
            if sim >= threshold:
                job.items.append(Dupe(name, best, v, sim, True, ext))
            elif sim >= threshold - UNSURE_SPAN:
                job.items.append(Dupe(name, best, v, sim, False, ext))
            else:
                job.other_cuts += 1
    job.items.sort(key=lambda d: (not d.sure, -d.drop.size))
    return job


# ---- texts -----------------------------------------------------------------------------------
def prefs_text(prefs: dict) -> str:
    langs = " vor ".join(LANG_NAME.get(x, x) for x in prefs.get("languages") or []) or "egal"
    res = prefs.get("resolutions") or []
    res_t = " vor ".join(("4K" if r == "2160" else f"{r}p" if r.isdigit() else r.upper()) for r in res[:3])
    order = "Sprache zuerst" if prefs.get("language_first", True) else "Auflösung zuerst"
    return (f"Behalten wird: {langs}; Auflösung {res_t}; {order}. Gleich ab {prefs.get('similarity')} % "
            "gleicher Laufzeit.")


def preview(job: DupeJob, prefs: dict) -> tuple[str, list[tuple[str, str]]]:
    sure = [d for d in job.items if d.sure]
    unsure = [d for d in job.items if not d.sure]
    lines = ["🧬 Duplikate in Plex", prefs_text(prefs)]
    if not job.items:
        lines.append("\n👍 Keine Duplikate gefunden.")
    if sure:
        lines.append(f"\n🗑 {len(sure)} doppelte Versionen · {size_text(sum(d.drop.size for d in sure))}:")
        lines += [f"• {d.name}: weg {d.drop.label} – bleibt {d.keep.label}" for d in sure[:PREVIEW_LINES]]
        if len(sure) > PREVIEW_LINES:
            lines.append(f"… und {len(sure) - PREVIEW_LINES} weitere")
    if unsure:
        lines.append(f"\n❓ {len(unsure)} ähnlich, aber nicht sicher gleich (andere Schnittfassung?) – "
                     "die frage ich einzeln:")
        lines += [f"• {d.name}: {d.similarity:.0f} % ähnlich" for d in unsure[:6]]
    if job.other_cuts:
        lines.append(f"\nℹ️ {job.other_cuts}× gleicher Titel mit deutlich anderer Laufzeit – lasse ich in Ruhe.")
    if job.no_details:
        lines.append(f"ℹ️ Bei {job.no_details} Titeln kenne ich die Tonspuren nicht (sehr große Bibliothek) – "
                     "dort entscheidet die Auflösung.")
    buttons: list[tuple[str, str]] = []
    if sure:
        buttons.append((f"🗑 Alle {len(sure)} sicheren löschen ({size_text(sum(d.drop.size for d in sure))})", "all"))
    if job.items:
        buttons += [("📋 Alle anzeigen (mit Dateien)", "list"), ("👆 Einzeln durchgehen", "one"),
                    ("✖️ Abbrechen", "no")]
    return "\n".join(lines), buttons


def _short(path: str, n: int = 70) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name if len(name) <= n else name[: n - 1] + "…"


def full_list(job: DupeJob) -> tuple[str, list[tuple[str, str]]]:
    """One page of all duplicates: what goes, what stays, with the file names. Nothing is deleted here."""
    pages = max(1, -(-len(job.items) // PAGE))
    job.page = max(0, min(job.page, pages - 1))
    start = job.page * PAGE
    lines = [f"📋 Duplikate {start + 1}–{min(start + PAGE, len(job.items))} von {len(job.items)}"
             + (f" (Seite {job.page + 1}/{pages})" if pages > 1 else "")]
    for i, d in enumerate(job.items[start:start + PAGE], start + 1):
        mark = "" if d.sure else f" ❓ nur {d.similarity:.0f} % gleich"
        lines.append(f"\n{i}. {d.name}{mark}")
        lines.append(f"   🗑 {d.drop.label}\n      {_short(d.drop.file)}")
        lines.append(f"   ✅ {d.keep.label}\n      {_short(d.keep.file)}")
    nav: list[tuple[str, str]] = []
    if job.page > 0:
        nav.append(("◀️ Zurück", "pg:prev"))
    if job.page < pages - 1:
        nav.append(("Weiter ▶️", "pg:next"))
    sure = [d for d in job.items if d.sure]
    buttons = nav + [("👆 Einzeln durchgehen", "one")]
    if sure:
        buttons.append((f"🗑 Alle {len(sure)} sicheren löschen ({size_text(sum(d.drop.size for d in sure))})", "all"))
    buttons.append(("✖️ Fertig", "no"))
    return "\n".join(lines), buttons


def one(job: DupeJob) -> tuple[str, list[tuple[str, str]]]:
    d = job.items[job.pos]
    head = f"🧬 {job.pos + 1}/{len(job.items)}: {d.name}"
    if not d.sure:
        head += f"\n❓ Nur {d.similarity:.0f} % gleiche Laufzeit – vielleicht eine andere Fassung."
    text = f"{head}\n🗑 Weg: {d.drop.label}\n✅ Bleibt: {d.keep.label}"
    return text, [("🗑 Schlechtere löschen", "del"), ("➡️ Beide behalten", "skip"), ("⏹ Stopp", "stop")]


def summary(job: DupeJob, cancelled: bool = False) -> str:
    if cancelled and not job.deleted:
        return "✖️ Abgebrochen – nichts gelöscht."
    lines = [f"🗑 {len(job.deleted)} doppelte Versionen gelöscht · {size_text(sum(d.drop.size for d in job.deleted))} frei."]
    if job.skipped:
        lines.append(f"➡️ Beide behalten: {job.skipped}")
    if job.failed:
        lines.append("⚠️ Nicht gelöscht: " + "; ".join(job.failed[:5]))
        if any("403" in f or "401" in f for f in job.failed):
            lines.append("Tipp: In Plex unter Einstellungen → Bibliothek „Löschen von Medien durch Clients "
                         "erlauben“ einschalten.")
    return "\n".join(lines)


def parse_resolutions(text: str) -> list[str]:
    """'1080p vor 4K' -> ['1080', '2160', ...rest]."""
    found = [norm_resolution(x) for x in re.findall(r"4k|uhd|2160p?|1080p?|720p?|576p?|480p?|sd", text.lower())]
    rest = [r for r in DEFAULT_PREFS["resolutions"] if r not in found]
    return list(dict.fromkeys(found)) + rest
