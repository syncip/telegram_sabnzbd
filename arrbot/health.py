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


async def run_checks(*, bot: Any, provider: LLMProvider | None, model: str, radarr: Any, sonarr: Any,
                     sab: Any, tmdb: Any, plex: Callable[[], Awaitable[str]] | None = None,
                     light: bool = False, builder: LLMProvider | None = None, builder_model: str = "",
                     chaptarr: Any = None) -> list[Check]:
    """light: only the services (no Telegram, no AI call) - for the regular monitoring."""
    async def telegram() -> str:
        me = await bot.get_me()
        return f"@{me.username}"

    def llm_check(prov: Any, name: str) -> Callable[[], Awaitable[str]]:
        async def check() -> str:
            # Small call (~100 tokens) that must use a tool: proves tool calling works with this model.
            ping = ToolSpec("ping", "Immer aufrufen.", {"type": "object", "properties": {}})
            turn = await prov.complete("Ruf das Werkzeug ping auf.", [Turn("user", text="ping")], [ping])
            tokens = (turn.usage or {}).get("input", 0) + (turn.usage or {}).get("output", 0)
            tools_ok = "" if any(c.name == "ping" for c in turn.tool_calls) else " – ⚠️ ruft keine Werkzeuge auf"
            return f"{name} antwortet{tools_ok}" + (f" ({tokens} Tokens)" if tokens else "")
        return check

    llm = llm_check(provider, model)

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

    jobs: list[Awaitable[Check]] = [] if light else [_timed("Telegram", telegram), _timed("KI", llm)]
    if builder is not None and not light:
        jobs.append(_timed("KI für Werkzeuge", llm_check(builder, builder_model)))
    skipped: list[Check] = []
    for name, svc, fn in (("Radarr", radarr, arr(radarr) if radarr else None),
                          ("Sonarr", sonarr, arr(sonarr) if sonarr else None),
                          ("Chaptarr", chaptarr, arr(chaptarr) if chaptarr else None),
                          ("SABnzbd", sab, sabnzbd), ("TMDB", tmdb, tmdb_check), ("Plex", plex, plex)):
        if svc is None:
            if light or name in ("Plex", "Chaptarr"):
                continue
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



class Monitor:
    """Regular check of the services. Tells the admins when one goes down and when it is back.

    A service counts as down only after FAILS checks in a row, so a single hiccup (restart, update)
    stays quiet.
    """

    FAILS = 2

    def __init__(self) -> None:
        self.fails: dict[str, int] = {}
        self.down: dict[str, tuple[float, str]] = {}  # name -> (since, reason)

    def update(self, checks: list[Check], now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        out = []
        for c in checks:
            if c.ok is None:
                continue
            if c.ok:
                self.fails.pop(c.name, None)
                if c.name in self.down:
                    since, _ = self.down.pop(c.name)
                    out.append(f"✅ {c.name} läuft wieder (war {_duration(now - since)} weg).")
                continue
            self.fails[c.name] = self.fails.get(c.name, 0) + 1
            if self.fails[c.name] == self.FAILS and c.name not in self.down:
                self.down[c.name] = (now, c.detail)
                out.append(f"🚨 {c.name} ist nicht erreichbar: {c.detail}")
        return out

    def status(self) -> str:
        if not self.down:
            return "🛡 Überwachung: alle Dienste laufen"
        return "🛡 Überwachung: " + ", ".join(f"{n} weg seit {_duration(time.time() - s)}"
                                            for n, (s, _) in self.down.items())


def _duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{max(m, 1)} Min."
    h, m = divmod(m, 60)
    return f"{h} Std. {m} Min." if h < 24 else f"{h // 24} Tagen"
