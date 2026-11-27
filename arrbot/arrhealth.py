"""🩺 Sonarr/Radarr's own warnings (System → Status) – read, report, and fix what can be fixed safely.

- New warnings go to the admins once; when one disappears, they hear "behoben".
- Harmless fixes run by themselves: an indexer or download client that failed is tested again and
  Sonarr/Radarr re-check their health (often that is all it takes after an outage).
- Fixes that change the configuration need a button: indexers with only main categories (5000 "TV")
  get their sub categories (5030 SD, 5040 HD …) – the reason Sonarr found nothing although the indexer had it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .arr import ArrError

log = logging.getLogger(__name__)

# Health "source" of Sonarr/Radarr -> what the bot does by itself
AUTO_FIX = {
    "IndexerStatusCheck": "indexers", "IndexerLongTermStatusCheck": "indexers",
    "IndexerSearchCheck": "indexers", "IndexerRssCheck": "indexers", "IndexerJackettAllCheck": None,
    "DownloadClientStatusCheck": "clients", "DownloadClientCheck": "clients",
}
# Short German explanation per source (the message of Sonarr/Radarr is shown too); {svc} = the service
EXPLAIN = {
    "IndexerStatusCheck": "Ein Indexer hatte Fehler und ist vorübergehend gesperrt – {svc} sucht dort nicht.",
    "IndexerLongTermStatusCheck": "Ein Indexer ist seit über 6 Stunden gesperrt.",
    "IndexerSearchCheck": "Kein Indexer kann gerade suchen – Suchen bringen nichts.",
    "IndexerRssCheck": "Kein Indexer liefert gerade neue Releases (RSS).",
    "DownloadClientStatusCheck": "Ein Download-Client hatte Fehler.",
    "DownloadClientCheck": "Der Download-Client (SABnzbd) ist nicht erreichbar – nichts kann geladen werden.",
    "RootFolderCheck": "Ein Medien-Ordner fehlt (Laufwerk nicht eingehängt? Pfad geändert?).",
    "RemotePathMappingCheck": "Fertige Downloads liegen an einem Pfad, den {svc} nicht sieht – Import klappt "
                              "nicht. Den Downloads-Ordner im {svc}-Container genauso einbinden wie bei SABnzbd "
                              "(oder Remote Path Mapping).",
    "ImportMechanismCheck": "Fertige Downloads werden nicht automatisch importiert.",
    "UpdateCheck": "Für {svc} gibt es ein Update.",
    "DiskSpaceCheck": "Wenig Speicherplatz.",
    "ApiKeyValidationCheck": "Der API-Key von {svc} ist zu kurz/ungültig.",
    "ProxyCheck": "Der Proxy funktioniert nicht.",
    "SystemTimeCheck": "Die Systemzeit stimmt nicht – Indexer lehnen Anfragen dann oft ab.",
    "IndexerCategories": "Nur Hauptkategorien eingetragen – viele Indexer liefern die Unterkategorien dann nicht mit.",
}
# Same sources, but a setting instead of a failure ("No indexers available with RSS sync enabled")
EXPLAIN_SETTING = {
    "IndexerSearchCheck": "Bei keinem Indexer ist die automatische Suche an – {svc} → Settings → Indexers → "
                          "„Enable Automatic Search“.",
    "IndexerRssCheck": "Bei keinem Indexer ist RSS an – neue Releases holt {svc} nicht von selbst "
                       "(Settings → Indexers → „Enable RSS“; mit Prowlarr dort „Full Sync“).",
}
WEIGHT = {"error": "❌", "warning": "⚠️", "notice": "ℹ️"}
CONFIRM = 2  # report an issue only after it was seen in so many rounds in a row (restarts, short hiccups stay quiet)


def is_setting(source: str, message: str) -> bool:
    """A switched-off option, not a failure: testing again does not help."""
    return source in EXPLAIN_SETTING and "enabled" in message.lower()


@dataclass
class Issue:
    service: str  # "Sonarr" | "Radarr" | "Chaptarr"
    source: str
    kind: str  # notice | warning | error
    message: str
    wiki: str = ""
    indexer_id: int | None = None  # IndexerCategories: which indexer

    @property
    def key(self) -> str:
        return f"{self.service}|{self.source}|{self.message}"

    @property
    def mute_key(self) -> str:
        return f"{self.service}|{self.source}"

    def text(self) -> str:
        out = f"{WEIGHT.get(self.kind, '⚠️')} {self.service}: {self.message}"
        explain = EXPLAIN_SETTING if is_setting(self.source, self.message) else EXPLAIN
        if self.source in explain:
            out += "\n" + explain[self.source].format(svc=self.service)
        return out


def _cats(ix: dict) -> list[int] | None:
    v = next((f.get("value") for f in ix.get("fields") or [] if f.get("name") == "categories"), None)
    return [int(c) for c in v] if isinstance(v, list) else None


def category_issues(service: str, indexers: list[dict]) -> list[Issue]:
    out = []
    for ix in indexers:
        if not (ix.get("enableAutomaticSearch") or ix.get("enableInteractiveSearch")):
            continue
        cats = _cats(ix)
        if cats and all(c % 100 == 0 for c in cats):
            out.append(Issue(service, "IndexerCategories", "warning",
                             f"Indexer {ix.get('name', '?')} sucht nur in Hauptkategorien ({', '.join(map(str, cats))})",
                             indexer_id=ix.get("id")))
    return out


async def issues(arr: Any) -> list[Issue]:
    raw = await arr._req("GET", "/health") or []
    out = [Issue(arr.kind, h.get("source", "?"), (h.get("type") or "warning").lower(), h.get("message", "?"),
                 str(h.get("wikiUrl") or "")) for h in raw if (h.get("type") or "").lower() != "ok"]
    if arr.kind == "Chaptarr":  # book categories (7000/8000) have no film/TV sub categories to add
        return out
    try:
        out += category_issues(arr.kind, await arr._req("GET", "/indexer") or [])
    except ArrError:
        pass
    return out


async def recheck(arr: Any, wait: float = 20.0) -> list[Issue]:
    """Let Sonarr/Radarr run their health check now and return what is left."""
    try:
        cmd = await arr._req("POST", "/command", json={"name": "CheckHealth"}) or {}
        loop = asyncio.get_running_loop()
        end = loop.time() + wait
        while cmd.get("id") and loop.time() < end:
            await asyncio.sleep(2)
            st = await arr._req("GET", f"/command/{cmd['id']}") or {}
            if st.get("status") in ("completed", "failed", "aborted", "cancelled"):
                break
    except ArrError as e:
        log.info("%s: CheckHealth ging nicht: %s", arr.kind, e)
    return await issues(arr)


async def auto_fix(arr: Any, issue: Issue, wait: float = 20.0) -> str | None:
    """Harmless fixes. Returns what was done and whether it helped, None if nothing can be done."""
    what = AUTO_FIX.get(issue.source)
    if not what or is_setting(issue.source, issue.message):
        return None
    path = "/indexer/testall" if what == "indexers" else "/downloadclient/testall"
    label = "Indexer" if what == "indexers" else "Download-Clients"
    try:
        results = await arr._req("POST", path) or []
    except ArrError as e:
        return f"🔧 {label} getestet: {e}"
    bad = [r for r in results if not r.get("isValid", True)]
    lines = [f"🔧 {label} neu getestet: {len(results) - len(bad)} von {len(results)} ok."]
    for r in bad[:3]:
        fails = "; ".join(str(f.get("errorMessage", f)) for f in r.get("validationFailures") or [])
        lines.append(f"   ❌ {r.get('id', '?')}: {fails or 'Fehler'}")
    left = await recheck(arr, wait)
    if any(x.key == issue.key for x in left):
        lines.append(f"Die Warnung ist noch da – {arr.kind} sperrt einen Indexer nach Fehlern eine Weile "
                     "(Backoff). Kommt sie wieder, liegt es am Indexer/Client selbst.")
    else:
        lines.append("✅ Danach ist die Warnung weg.")
    return "\n".join(lines)


# Sub categories added if Sonarr/Radarr cannot tell the indexer's own list: SD, HD, 720p, 1080p, UHD, WEB-DL
FALLBACK_SUBS = (10, 30, 40, 41, 42, 45)


async def add_subcategories(arr: Any, indexer_id: int) -> str:
    """Add the sub categories of the main categories set on an indexer (button, changes the config)."""
    ix = await arr._req("GET", f"/indexer/{indexer_id}")
    cats = _cats(ix) or []
    parents = [c for c in cats if c % 100 == 0]
    if not parents:
        return f"Indexer {ix.get('name', '?')}: nichts zu tun."
    subs: list[int] = []
    try:  # the indexer's own category list, like the drop-down in Sonarr
        res = await arr._req("POST", "/indexer/action/newznabCategories", json=ix) or {}
        for o in res.get("options") or []:
            if o.get("parentValue") in parents and o.get("value") not in cats:
                subs.append(int(o["value"]))
    except (ArrError, TypeError, ValueError, KeyError):
        subs = []
    source = "laut Indexer"
    if not subs:
        subs = [p + d for p in parents for d in FALLBACK_SUBS if p + d not in cats]
        source = "Standard-Unterkategorien"
    for f in ix.get("fields") or []:
        if f.get("name") == "categories":
            f["value"] = sorted(set(cats + subs))
    await arr._req("PUT", f"/indexer/{indexer_id}", json=ix)
    return (f"✅ Indexer {ix.get('name', '?')}: {len(subs)} Unterkategorien eingetragen ({source}): "
            f"{', '.join(map(str, sorted(subs)))}. Am besten jetzt einmal „such nochmal“ für das, was fehlte.")


class HealthWatch:
    """Remembers what was reported (data/arr_health.json), so a restart does not repeat everything."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "arr_health.json"
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if "known" not in raw:  # before 1.22: only the reported issues
            raw = {"known": {k: dict(v, told=True) for k, v in raw.items()}}
        self.known: dict[str, dict] = raw["known"]
        self.muted: set[str] = set(raw.get("muted") or [])  # "Service|source": until the issue is gone

    def mute(self, service: str, source: str) -> None:
        self.muted.add(f"{service}|{source}")
        self._save()

    def _save(self) -> None:
        try:
            data = {"known": self.known, "muted": sorted(self.muted)}
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("arr_health.json nicht geschrieben: %s", e)

    async def poll(self, services: list[Any]) -> tuple[list[Issue], list[dict]]:
        """(new issues, resolved ones). A service that cannot be asked keeps its known issues.

        New = seen in CONFIRM rounds in a row and not muted; resolved = only what was reported.
        """
        new, gone = [], []
        changed = False
        for arr in services:
            try:
                now = await issues(arr)
            except ArrError as e:
                log.info("%s: Health nicht abrufbar: %s", arr.kind, e)
                continue
            keys = {i.key for i in now}
            for i in now:
                k = self.known.setdefault(i.key, {"service": i.service, "source": i.source, "message": i.message,
                                                  "seen": 0, "told": False})
                if k.get("told"):
                    continue
                k["seen"] = k.get("seen", 0) + 1
                changed = True
                if k["seen"] >= CONFIRM and i.mute_key not in self.muted:
                    k["told"] = True
                    new.append(i)
            for k, v in list(self.known.items()):
                if v.get("service") == arr.kind and k not in keys:
                    self.known.pop(k)
                    changed = True
                    if v.get("told"):
                        gone.append(v)
            still = {f"{v.get('service')}|{v.get('source')}" for v in self.known.values()}
            for m in [m for m in self.muted if m.startswith(arr.kind + "|") and m not in still]:
                self.muted.discard(m)  # fixed: report it again if it comes back
                changed = True
        if changed:
            self._save()
        return new, gone
