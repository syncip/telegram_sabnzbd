"""🗣 Only in another language: the search found releases, but none in German (all rejected by the profile).

Then the bot says which languages there are ("nur Koreanisch und Englisch"), that the chosen quality
profile probably does not fit, and offers one button per language (Koreanisch, Englisch, Multi). A tap
grabs the best release of that language directly (like the download button in Sonarr/Radarr, past the
profile): for a series season the season pack, else the best release per episode.

Also when German releases exist but nothing was loaded: `choices` offers one button per quality and language
("⬇️ 720p Deutsch laden", "⬇️ 1080p Englisch laden", "⬇️ 1080p Multi (DE/EN) laden").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .german import LANG_DE, _is_german_release, _langs
from .manage import BAD_REJECTIONS

MULTI = re.compile(r"[.\s_-](multi|dual|dl\.multi)[.\s_-]", re.I)
SOFT = ("language", "custom format", "score")  # rejections that only come from the language wish
MAX_LANGS = 4
MAX_CHOICES = 6
SHORT = {"German": "DE", "English": "EN", "French": "FR", "Spanish": "ES", "Italian": "IT", "Japanese": "JA",
         "Korean": "KO", "Russian": "RU", "Polish": "PL", "Turkish": "TR", "Dutch": "NL"}


def label(r: dict) -> str:
    langs = [x for x in _langs(r) if x and x.lower() != "unknown"]
    if len(langs) > 1 or MULTI.search("." + (r.get("title") or "") + "."):
        return "Multi"
    return LANG_DE.get(langs[0], langs[0]) if langs else "Sprache unbekannt"


def _texts(r: dict) -> str:
    raw = r.get("rejections") or []
    return " ".join(x.get("reason", "") if isinstance(x, dict) else str(x) for x in raw).lower()


def _grabbable(r: dict) -> bool:
    if not r.get("guid") or r.get("indexerId") is None:
        return False
    low = _texts(r)
    return not any(b in low for b in BAD_REJECTIONS) and "unable to identify" not in low


def _lang_only(r: dict) -> bool:
    """Rejected only for language/custom format: quality, size etc. fit the profile."""
    if not r.get("rejected"):
        return True
    raw = r.get("rejections") or []
    texts = [(x.get("reason", "") if isinstance(x, dict) else str(x)).lower() for x in raw]
    return bool(texts) and all(any(s in t for s in SOFT) for t in texts)


def _res(r: dict) -> int:
    return int(((r.get("quality") or {}).get("quality") or {}).get("resolution") or 0)


def _rank(r: dict, target: int) -> tuple:
    res = _res(r)
    fit = -abs(target - res) if res <= target else -10_000 - res  # as close to the wish as possible, not above
    return (_lang_only(r), fit, r.get("seeders") or 0, r.get("qualityWeight") or 0, -(r.get("size") or 0))


@dataclass
class LangOffer:
    kind: str  # movie | series
    arr_id: int
    season: int | None
    chat_id: int
    title: str
    profile: str
    groups: dict[str, list[dict]] = field(default_factory=dict)  # label -> releases
    targets: dict[str, int] = field(default_factory=dict)  # label -> resolution (quality choices only)
    buttons: dict[str, str] = field(default_factory=dict)  # label -> button text, if not "Auf <label> laden"

    def button(self, label_: str) -> str:
        return self.buttons.get(label_) or f"⬇️ Auf {label_} laden"

    def done(self, label_: str) -> str:
        return f"in {label_}" if label_ in self.targets else f"auf {label_}"

    def text(self) -> str:
        names = list(self.groups)
        langs = " und ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)
        what = self.title + (f" (Staffel {self.season})" if self.season is not None else "")
        prof = f"„{self.profile}“" if self.profile else "dein Profil"
        return (f"🗣 {what} gibt es bei deinen Indexern nur auf {langs} – nicht auf Deutsch.\n"
                f"Dein Profil {prof} passt dafür im Zweifel nicht, darum hat "
                f"{'Radarr' if self.kind == 'movie' else 'Sonarr'} nichts geladen.\n"
                "In welcher Sprache soll ich es laden? Oder lieber wieder löschen bzw. auf Deutsch warten?")


def build(kind: str, arr_id: int, season: int | None, chat_id: int, title: str, profile: str,
          releases: list[dict]) -> LangOffer | None:
    """None when there is nothing to offer: no releases, a German one exists, or one would be taken anyway."""
    if not releases or any(_is_german_release(r) for r in releases):
        return None
    if any(not r.get("rejected") for r in releases):
        return None  # Sonarr/Radarr may take this one: another problem (or just timing)
    groups: dict[str, list[dict]] = {}
    for r in releases:
        if _grabbable(r):
            groups.setdefault(label(r), []).append(r)
    if not groups:
        return None
    order = sorted(groups, key=lambda k: (k == "Sprache unbekannt", -len(groups[k])))[:MAX_LANGS]
    return LangOffer(kind, arr_id, season, chat_id, title, profile, {k: groups[k] for k in order})


def _res_label(r: dict) -> str:
    res = _res(r)
    if res >= 2000:
        return "4K"
    if res:
        return f"{res}p"
    return ((r.get("quality") or {}).get("quality") or {}).get("name") or "SD"


def choices(kind: str, arr_id: int, season: int | None, chat_id: int, title: str, profile: str,
            releases: list[dict]) -> LangOffer | None:
    """Every quality/language there is, one button each: German first, then by resolution and count."""
    groups: dict[str, list[dict]] = {}
    targets: dict[str, int] = {}
    langs: dict[str, list[list[str]]] = {}
    for r in releases:
        if not _grabbable(r):
            continue
        lang = label(r)
        if lang != "Multi" and _is_german_release(r):
            lang = "Deutsch"
        key = f"{_res_label(r)} {lang}"
        groups.setdefault(key, []).append(r)
        targets[key] = max(targets.get(key, 0), _res(r))
        langs.setdefault(key, []).append([x for x in _langs(r) if x and x.lower() != "unknown"])
    if not groups:
        return None
    order = sorted(groups, key=lambda k: (not k.endswith(" Deutsch"), k.endswith("Sprache unbekannt"),
                                          -targets[k], -len(groups[k])))[:MAX_CHOICES]
    buttons = {}
    for k in order:
        text = k
        if k.endswith(" Multi"):  # which languages most of them have
            seen = langs[k]
            common = [x for x in SHORT if sum(x in s for s in seen) * 2 >= len(seen)][:3]
            if common:
                text += " (" + "/".join(SHORT[x] for x in common) + ")"
        buttons[k] = f"⬇️ {text} laden"
    return LangOffer(kind, arr_id, season, chat_id, title, profile, {k: groups[k] for k in order},
                     {k: targets[k] or 1080 for k in order}, buttons)


def pick(releases: list[dict], target: int = 1080) -> list[dict]:
    """What to grab: the best season pack, else the best release per episode (movies: the best one)."""
    if not releases:
        return []
    packs = [r for r in releases if r.get("fullSeason")]
    if packs:
        return [max(packs, key=lambda r: _rank(r, target))]
    if not any(r.get("episodeNumbers") for r in releases):
        return [max(releases, key=lambda r: _rank(r, target))]
    best: dict[tuple, dict] = {}
    for r in releases:
        key = tuple(r.get("episodeNumbers") or ())
        if key and (key not in best or _rank(r, target) > _rank(best[key], target)):
            best[key] = r
    # no double episodes: bigger bundles first, then single episodes that are still missing
    out, covered = [], set()
    for key in sorted(best, key=lambda k: -len(k)):
        if not covered & set(key):
            out.append(best[key])
            covered |= set(key)
    return sorted(out, key=lambda r: tuple(r.get("episodeNumbers") or ()))
