"""Admin: create quality profiles from plain language ("4K nur Englisch", "1080p Deutsch + Englisch").

The AI only turns the sentence into a few fields (resolution range, languages). The profile itself is
built here, shown for confirmation and then created in Radarr and/or Sonarr:
  - qualities: copied from an existing profile, allowed = inside the resolution range, best one = cutoff
  - languages: custom formats with a language condition ("Sprache: Deutsch"), created if missing.
    Required languages get a high score and the profile's minimum score demands all of them; preferred
    ones a small bonus; "nur" punishes other common languages so they are never grabbed.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from .arr import ArrError

log = logging.getLogger(__name__)

REQUIRED, PREFERRED, BLOCK = 1000, 100, -10000
COMMON = ["German", "English", "French", "Spanish", "Italian"]
NAMES_DE = {"German": "Deutsch", "English": "Englisch", "French": "Französisch", "Spanish": "Spanisch",
            "Italian": "Italienisch", "Japanese": "Japanisch", "Original": "Originalsprache"}
ALIASES = {
    "deutsch": "German", "german": "German", "de": "German", "englisch": "English", "english": "English",
    "en": "English", "französisch": "French", "french": "French", "spanisch": "Spanish", "spanish": "Spanish",
    "italienisch": "Italian", "italian": "Italian", "japanisch": "Japanese", "japanese": "Japanese",
    "original": "Original", "originalsprache": "Original", "originalton": "Original", "ov": "Original",
}
RESOLUTIONS = [480, 576, 720, 1080, 2160]


def lang(name: str) -> str | None:
    return ALIASES.get(name.strip().lower())


@dataclass
class ProfileWish:
    name: str
    services: list[str]  # radarr, sonarr
    min_res: int = 0
    max_res: int = 2160
    required: list[str] = field(default_factory=list)
    preferred: list[str] = field(default_factory=list)
    only: bool = False  # no languages besides required + preferred
    unknown: list[str] = field(default_factory=list)

    def summary(self) -> str:
        res = (f"{self.min_res}p bis {self.max_res}p" if self.min_res and self.min_res != self.max_res
               else f"{self.max_res}p" if self.min_res == self.max_res else f"bis {self.max_res}p")
        res = res.replace("2160p", "4K")
        out = [f"🎛 Neues Qualitätsprofil „{self.name}“",
               f"📺 Auflösung: {res}",
               "🗣 Pflicht: " + (" + ".join(NAMES_DE.get(x, x) for x in self.required) or "keine Sprache vorgegeben")]
        if self.preferred:
            out.append("➕ Bevorzugt: " + ", ".join(NAMES_DE.get(x, x) for x in self.preferred))
        if self.only:
            other = [NAMES_DE[x] for x in COMMON if x not in self.required + self.preferred]
            out.append("🚫 Nie: " + ", ".join(other))
        out.append("🧩 Für: " + " und ".join("Filme (Radarr)" if s == "radarr" else "Serien (Sonarr)" for s in self.services))
        if self.unknown:
            out.append("⚠️ Unbekannte Sprache ignoriert: " + ", ".join(self.unknown))
        return "\n".join(out)


def make_wish(name: str, service: str | None, min_resolution: int | None, max_resolution: int | None,
              required_languages: list[str] | None, preferred_languages: list[str] | None,
              only: bool | None) -> ProfileWish:
    def snap(v: int | None, default: int) -> int:
        if not v:
            return default
        return min(RESOLUTIONS, key=lambda r: abs(r - int(v)))

    services = ["radarr", "sonarr"] if service in (None, "", "both") else [service]
    w = ProfileWish(name.strip()[:50] or "Neues Profil", services, snap(min_resolution, 0) if min_resolution else 0,
                    snap(max_resolution, 2160), only=bool(only))
    for raw in required_languages or []:
        (w.required.append(lang(raw)) if lang(raw) else w.unknown.append(raw))
    for raw in preferred_languages or []:
        g = lang(raw)
        if g and g not in w.required:
            w.preferred.append(g)
        elif not g:
            w.unknown.append(raw)
    w.required = list(dict.fromkeys(w.required))
    w.preferred = list(dict.fromkeys(w.preferred))
    if w.min_res > w.max_res:
        w.min_res, w.max_res = w.max_res, w.min_res
    return w


# ---- building -----------------------------------------------------------------------------------
def _res(item: dict) -> int:
    return int(((item.get("quality") or {}).get("resolution")) or 0)


def _allow(items: list[dict], lo: int, hi: int) -> None:
    for it in items:
        if it.get("items"):  # quality group
            _allow(it["items"], lo, hi)
            it["allowed"] = any(x.get("allowed") for x in it["items"])
        else:
            r = _res(it)
            it["allowed"] = bool(r) and lo <= r <= hi


def _cutoff(items: list[dict]) -> int | None:
    """Id of the best allowed entry (items are ordered worst -> best)."""
    best = None
    for it in items:
        if it.get("allowed"):
            best = it.get("id") if it.get("items") else (it.get("quality") or {}).get("id")
    return best


async def _language_ids(svc: Any) -> dict[str, int]:
    langs = await svc._req("GET", "/language") or []
    return {x.get("name"): x.get("id") for x in langs if x.get("name")}


async def _language_format(svc: Any, language: str, ids: dict[str, int], formats: list[dict]) -> dict:
    name = f"Sprache: {NAMES_DE.get(language, language)}"
    for cf in formats:
        if cf.get("name") == name:
            return cf
    if language not in ids:
        raise ArrError(f"{svc.kind} kennt die Sprache {NAMES_DE.get(language, language)} nicht.")
    body = {"name": name, "includeCustomFormatWhenRenaming": False, "specifications": [{
        "name": NAMES_DE.get(language, language), "implementation": "LanguageSpecification",
        "negate": False, "required": True, "fields": [{"name": "value", "value": ids[language]}]}]}
    cf = await svc._req("POST", "/customformat", json=body)
    formats.append(cf)
    log.info("%s: Custom Format %r angelegt", svc.kind, name)
    return cf


async def create(svc: Any, w: ProfileWish) -> str:
    """Create the profile in one service. Returns a German result line."""
    profiles = await svc.quality_profiles()
    if any(p["name"].lower() == w.name.lower() for p in profiles):
        return f"⚠️ {svc.kind}: Ein Profil „{w.name}“ gibt es schon – nichts geändert."
    if not profiles:
        return f"⚠️ {svc.kind}: Kein Profil als Vorlage vorhanden."
    base = copy.deepcopy(await svc._req("GET", f"/qualityprofile/{profiles[0]['id']}"))
    _allow(base["items"], w.min_res, w.max_res)
    cutoff = _cutoff(base["items"])
    if cutoff is None:
        return f"⚠️ {svc.kind}: Keine Qualität passt zu {w.min_res}–{w.max_res}p."
    profile = {k: v for k, v in base.items() if k != "id"}
    profile.update(name=w.name, upgradeAllowed=True, cutoff=cutoff)
    notes = []
    langs = w.required + w.preferred
    if langs or w.only:
        try:
            formats = await svc._req("GET", "/customformat") or []
            ids = await _language_ids(svc)
        except ArrError:
            formats, ids = None, {}
        if formats is None:
            notes.append("Sprachen nicht gesetzt: diese Version kennt keine Custom Formats")
        else:
            scores: dict[int, int] = {}
            for x in w.required:
                scores[(await _language_format(svc, x, ids, formats))["id"]] = REQUIRED
            for x in w.preferred:
                try:
                    scores[(await _language_format(svc, x, ids, formats))["id"]] = PREFERRED
                except ArrError as e:  # e.g. "Original" in an older Sonarr
                    notes.append(str(e))
            if w.only:
                for x in COMMON:
                    if x not in langs:
                        scores[(await _language_format(svc, x, ids, formats))["id"]] = BLOCK
            profile["formatItems"] = [{"format": cf["id"], "name": cf["name"], "score": scores.get(cf["id"], 0)}
                                      for cf in formats]
            profile["minFormatScore"] = REQUIRED * len(w.required)
            profile["cutoffFormatScore"] = REQUIRED * len(w.required) + PREFERRED * len(w.preferred)
            if "language" in profile and ids.get("Any") is not None:
                profile["language"] = {"id": ids["Any"], "name": "Any"}  # Radarr: languages via formats
    created = await svc._req("POST", "/qualityprofile", json=profile)
    svc._profiles = None  # reload the list next time
    extra = f" ({'; '.join(notes)})" if notes else ""
    return f"✅ {svc.kind}: Profil „{created.get('name', w.name)}“ angelegt{extra}."


def max_resolution(profile: dict) -> int:
    """Highest resolution a profile may download (0 = unknown)."""
    best = 0

    def walk(items: list[dict], allowed: bool) -> None:
        nonlocal best
        for i in items:
            ok = allowed or bool(i.get("allowed"))
            if i.get("items"):
                walk(i["items"], ok)
            elif ok:
                best = max(best, _res(i))

    walk(profile.get("items") or [], False)
    return best
