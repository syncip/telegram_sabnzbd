"""SABnzbd queue: is a download running, how fast, and when will it be done."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

import httpx

log = logging.getLogger(__name__)


class SabError(Exception):
    pass


@dataclass
class SabSlot:
    nzo_id: str
    name: str
    status: str  # Downloading, Queued, Paused, Fetching, Grabbing, Propagating, ...
    percent: int
    mb_total: float
    mb_left: float
    timeleft: timedelta | None
    position: int

    def finish_at(self, now: datetime) -> datetime | None:
        return now + self.timeleft if self.timeleft and self.status != "Paused" else None


@dataclass
class SabQueue:
    paused: bool
    speed_kbps: float
    slots: dict[str, SabSlot]  # by nzo_id

    @property
    def speed_text(self) -> str:
        mb = self.speed_kbps / 1024
        return f"{mb:.1f} MB/s" if mb >= 0.1 else "steht"


def parse_timeleft(raw: str | None) -> timedelta | None:
    """SABnzbd uses 'h:mm:ss' and 'd:hh:mm:ss'."""
    if not raw:
        return None
    try:
        parts = [int(p) for p in str(raw).split(":")]
    except ValueError:
        return None
    secs = 0
    for factor, value in zip((1, 60, 3600, 86400), reversed(parts)):
        secs += factor * value
    return timedelta(seconds=secs) if secs > 0 else None


def _float(v: object) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


class SABnzbd:
    def __init__(self, url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key
        self._http = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=httpx.Timeout(15.0), transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    async def queue(self) -> SabQueue:
        try:
            r = await self._http.get(
                "/api", params={"mode": "queue", "output": "json", "apikey": self.api_key, "limit": 200}
            )
        except httpx.HTTPError as e:
            raise SabError(f"SABnzbd nicht erreichbar: {e}") from e
        if r.status_code >= 400:
            raise SabError(f"SABnzbd antwortet mit HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise SabError("SABnzbd liefert kein JSON (URL richtig?)") from e
        if "error" in data:
            raise SabError(f"SABnzbd: {data['error']}")
        q = data.get("queue") or {}
        slots = {}
        for s in q.get("slots") or []:
            slot = SabSlot(
                nzo_id=s.get("nzo_id", ""),
                name=s.get("filename", "?"),
                status=s.get("status", "?"),
                percent=int(_float(s.get("percentage"))),
                mb_total=_float(s.get("mb")),
                mb_left=_float(s.get("mbleft")),
                timeleft=parse_timeleft(s.get("timeleft")),
                position=int(_float(s.get("index"))),
            )
            slots[slot.nzo_id] = slot
        return SabQueue(paused=bool(q.get("paused")), speed_kbps=_float(q.get("kbpersec")), slots=slots)


def size_text(mb: float) -> str:
    return f"{mb / 1024:.1f} GB".replace(".", ",") if mb >= 1024 else f"{mb:.0f} MB"


def eta_text(slot: SabSlot, now: datetime, tz: tzinfo, queue_paused: bool = False) -> str:
    """Human text like 'fertig ca. 21:45 Uhr (noch 23 min)'."""
    if queue_paused or slot.status == "Paused":
        return "pausiert in SABnzbd"
    end = slot.finish_at(now)
    if not end or not slot.timeleft:
        return "Restzeit noch unbekannt"
    end_local = end.astimezone(tz)
    day = ""
    days = (end_local.date() - now.astimezone(tz).date()).days
    if days == 1:
        day = "morgen "
    elif days > 1:
        day = end_local.strftime("%d.%m. ")
    mins = int(slot.timeleft.total_seconds() // 60)
    left = f"{mins} min" if mins < 60 else f"{mins // 60} h {mins % 60:02d} min"
    return f"fertig ca. {day}{end_local:%H:%M} Uhr (noch {left})"
