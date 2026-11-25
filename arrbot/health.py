"""/check: is every API reachable and is the key accepted? One line per service."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .llm import LLMProvider, ToolSpec, Turn

log = logging.getLogger(__name__)

TIMEOUT = 20


@dataclass
class Check:
    name: str
    ok: bool | None  # None = not configured
    detail: str
    ms: int = 0

    @property
    def line(self) -> str:
        icon = {True: "✅", False: "❌", None: "⚪"}[self.ok]
        ms = f" <i>({self.ms} ms)</i>" if self.ok is not None and self.ms else ""
        return f"{icon} <b>{html.escape(self.name)}</b>: {html.escape(self.detail)}{ms}"


async def _timed(name: str, fn: Callable[[], Awaitable[str]]) -> Check:
    started = time.monotonic()
    try:
        detail = await asyncio.wait_for(fn(), TIMEOUT)
        return Check(name, True, detail, int((time.monotonic() - started) * 1000))
    except asyncio.TimeoutError:
        return Check(name, False, f"keine Antwort nach {TIMEOUT} s", int((time.monotonic() - started) * 1000))
    except Exception as e:  # noqa: BLE001 - every failure is a result here
        log.warning("Check %s fehlgeschlagen: %s", name, e)
        return Check(name, False, _short(e), int((time.monotonic() - started) * 1000))


def _short(e: Exception) -> str:
    text = str(e) or type(e).__name__
    low = text.lower()
    if "401" in low or "unauthorized" in low or "invalid api key" in low or "lehnt den api-key ab" in low:
        return "API-Key falsch (401)"
    if "403" in low:
        return "Zugriff verweigert (403)"
    if "nicht erreichbar" in low or "connect" in low or "name or service" in low or "getaddrinfo" in low:
        return "nicht erreichbar – URL/Netzwerk prüfen"
    return text[:150]


async def run_checks(*, bot: Any, provider: LLMProvider, model: str, radarr: Any, sonarr: Any,
                     sab: Any, tmdb: Any) -> list[Check]:
    async def telegram() -> str:
        me = await bot.get_me()
        return f"@{me.username}"

    async def llm() -> str:
        # Smallest possible call (~60 tokens). The dummy tool keeps providers happy that reject empty lists.
        ping = ToolSpec("ping", "Nicht benutzen.", {"type": "object", "properties": {}})
        turn = await provider.complete("Antworte nur mit: ok", [Turn("user", text="ping")], [ping])
        tokens = (turn.usage or {}).get("input", 0) + (turn.usage or {}).get("output", 0)
        return f"{model} antwortet" + (f" ({tokens} Tokens)" if tokens else "")

    def arr(svc: Any) -> Callable[[], Awaitable[str]]:
        async def check() -> str:
            status = await svc._req("GET", "/system/status")
            profiles = await svc.quality_profiles()
            return f"Version {status.get('version', '?')}, {len(profiles)} Qualitätsprofile"
        return check

    async def sabnzbd() -> str:
        q = await sab.queue()
        state = "pausiert" if q.paused else f"{len(q.slots)} in der Warteschlange"
        r = await sab._http.get("/api", params={"mode": "version", "output": "json", "apikey": sab.api_key})
        version = r.json().get("version", "?") if r.status_code < 400 else "?"
        return f"Version {version}, {state}"

    async def tmdb_check() -> str:
        info = await tmdb._get("/movie/603", language="de-DE")  # "Matrix": stable test title
        return f"Key gültig, deutsche Titel kommen an („{info.get('title')}“)"

    jobs: list[Awaitable[Check]] = [_timed("Telegram", telegram), _timed("KI", llm)]
    skipped: list[Check] = []
    for name, svc, fn in (("Radarr", radarr, arr(radarr) if radarr else None),
                          ("Sonarr", sonarr, arr(sonarr) if sonarr else None),
                          ("SABnzbd", sab, sabnzbd), ("TMDB", tmdb, tmdb_check)):
        if svc is None:
            hint = {"TMDB": "nicht eingerichtet – ohne Key keine deutschen Titel, Top-Listen, Vorschläge",
                    "SABnzbd": "nicht eingerichtet – ohne SABnzbd keine Fertig-Uhrzeit"}.get(name, "nicht eingerichtet")
            skipped.append(Check(name, None, hint))
        else:
            jobs.append(_timed(name, fn))
    return list(await asyncio.gather(*jobs)) + skipped


def render(checks: list[Check]) -> str:
    bad = sum(1 for c in checks if c.ok is False)
    head = "🩺 <b>API-Check</b> – " + ("alles in Ordnung ✅" if not bad else f"{bad} Problem{'e' if bad > 1 else ''} ❌")
    return "\n".join([head, ""] + [c.line for c in checks])

