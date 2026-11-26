"""🩺 Why did a search load nothing? Checked in code right after Sonarr/Radarr report the search done.

Looks at what is usually wrong, in this order:
- title/season/episodes not monitored (Sonarr/Radarr then skip them without a word)
- the indexers return nothing at all – often only the main categories (5000 "TV", 5100 "TV-DE") are set and
  the indexer does not include their sub categories (SD 5030, HD 5040 …)
- there are releases, but all are rejected: which qualities/languages exist and why they were rejected
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .arr import ArrError

GRABBED = re.compile(r"(\d+)\s+reports?\s+(?:downloaded|grabbed)", re.I)

# Rejection texts of Sonarr/Radarr -> short German reason
REASONS = [
    ("not wanted in profile", "Qualität nicht im Profil"),
    ("quality", "Qualität passt nicht zum Profil"),
    ("language", "Sprache nicht gewünscht"),
    ("custom format", "Custom-Format-Wertung zu niedrig"),
    ("score", "Custom-Format-Wertung zu niedrig"),
    ("size", "Größe außerhalb der Grenzen"),
    ("too large", "zu groß"),
    ("too small", "zu klein"),
    ("not monitored", "nicht überwacht"),
    ("unknown series", "Sonarr erkennt die Serie im Namen nicht"),
    ("unknown movie", "Radarr erkennt den Film im Namen nicht"),
    ("blocklist", "auf der Sperrliste"),
    ("already", "schon vorhanden/geladen"),
    ("existing file", "vorhandene Datei ist gleich gut oder besser"),
    ("cutoff", "Ziel-Qualität schon erreicht"),
    ("retention", "zu alt für deinen Usenet-Anbieter"),
    ("minimum age", "noch zu neu (Mindestalter)"),
    ("season pack", "Staffelpaket passt nicht"),
    ("does not match", "passt nicht zur gesuchten Folge/Staffel"),
]


def grabbed(message: str | None) -> int | None:
    """Number of loaded releases from the command message, None if it does not say."""
    m = GRABBED.search(message or "")
    return int(m.group(1)) if m else None


def _reason(r: dict) -> str:
    raw = r.get("rejections") or []
    texts = [x.get("reason", "") if isinstance(x, dict) else str(x) for x in raw]
    low = " ".join(texts).lower()
    for key, de in REASONS:
        if key in low:
            return de
    return texts[0][:80] if texts else "abgelehnt"


def _quality(r: dict) -> str:
    q = ((r.get("quality") or {}).get("quality") or {}).get("name") or "?"
    langs = [x.get("name", "") for x in r.get("languages") or [] if isinstance(x, dict)]
    lang = "Deutsch" if any(x.lower() == "german" for x in langs) else (langs[0] if langs else "")
    lang = {"English": "Englisch", "Unknown": "Sprache?"}.get(lang, lang)
    return f"{q}{f' ({lang})' if lang else ''}"


def summarize_releases(releases: list[dict], limit: int = 4) -> list[str]:
    if not releases:
        return []
    ok = [r for r in releases if not r.get("rejected")]
    bad = [r for r in releases if r.get("rejected")]
    out = [f"🔎 Die Indexer liefern {len(releases)} Release{'s' if len(releases) != 1 else ''}"
           f" – {len(ok)} passend, {len(bad)} abgelehnt."]
    if ok:
        avail = Counter(_quality(r) for r in ok).most_common(limit)
        out.append("✅ Passend wären: " + ", ".join(f"{n}× {q}" for q, n in avail)
                   + ". Sonarr/Radarr sollte eins davon nehmen – vielleicht lief schon ein Download "
                     "oder die Suche war vor dem Überwachen fertig. „Such nochmal“ hilft meist.")
    if bad:
        why = Counter(_reason(r) for r in bad).most_common(3)
        avail = Counter(_quality(r) for r in bad).most_common(limit)
        out.append("❌ Abgelehnt: " + ", ".join(f"{n}× {q}" for q, n in avail))
        out.append("   Gründe: " + "; ".join(f"{w} ({n}×)" for w, n in why))
    return out


def category_hint(indexers: list[dict]) -> str | None:
    """Indexers that only have main categories (x000/x100 …) set: their sub categories may be missing."""
    names = []
    for ix in indexers:
        if not (ix.get("enableAutomaticSearch") or ix.get("enableInteractiveSearch")):
            continue
        cats = next((f.get("value") for f in ix.get("fields") or [] if f.get("name") == "categories"), None)
        if isinstance(cats, list) and cats and all(int(c) % 100 == 0 for c in cats):
            names.append(f"{ix.get('name', '?')} ({', '.join(map(str, cats))})")
    if not names:
        return None
    return ("🗂 Bei " + "; ".join(names) + " sind nur Hauptkategorien eingetragen. Viele Indexer liefern die "
            "Unterkategorien (z. B. SD 5030, HD 5040, 1080p 5042) dann nicht mit – in Settings → Indexers anhaken.")


async def why_nothing(arr: Any, kind: str, item_id: int, season: int | None = None,
                      episode_id: int | None = None, keep: list | None = None) -> str:
    """Text for "the search loaded nothing": monitoring, indexer categories, available releases."""
    lines: list[str] = []
    item = await arr.get(item_id)
    title = item.get("title", "?")
    if not item.get("monitored", True):
        lines.append(f"👁 {title} ist nicht überwacht – dann lädt {arr.kind} nichts. "
                     "„Such nochmal“ schaltet es ein.")
    if kind == "series" and season is not None:
        s = next((x for x in item.get("seasons") or [] if x.get("seasonNumber") == season), None)
        if s is not None and not s.get("monitored", True):
            lines.append(f"👁 Staffel {season} ist nicht überwacht.")
    try:
        if kind == "movie":
            releases = await arr.releases(movieId=item_id)
        elif episode_id is not None:
            releases = await arr.releases(episodeId=episode_id)
        else:
            releases = await arr.releases(seriesId=item_id, seasonNumber=season if season is not None else 1)
    except ArrError as e:
        releases = None
        lines.append(f"⚠️ Die Indexer-Abfrage ging nicht: {e}")
    if releases is not None:
        if keep is not None:
            keep.extend(releases)  # the caller looks at them again (only other languages?)
        if releases:
            lines += summarize_releases(releases)
        else:
            what = f"Staffel {season}" if season is not None else "den Titel"
            lines.append(f"🔎 Deine Indexer liefern für {what} gar nichts – auch nichts Abgelehntes.")
            try:
                hint = category_hint(await arr._req("GET", "/indexer") or [])
            except ArrError:
                hint = None
            lines.append(hint or "Entweder hat der Indexer es nicht, oder er findet es unter einem anderen "
                                 "Namen bzw. in einer Kategorie, die nicht eingetragen ist.")
    return "\n".join(lines)
