"""🎧 Listen in: is a file really German? Checks the spoken language instead of the track tags.

Track tags are often wrong (old German DVD rips tag German as English). Here every audio track of a
file gets 20-second clips at 25, 50 and 72 % of the runtime (never intro or credits; ffmpeg, local, free); only those clips go to OpenAI's
speech recognition, which says which language is spoken. About 0.2 cent per clip.

Needs: ffmpeg/ffprobe in the container, the media folders mounted (read-only is enough) and an
OpenAI key. MEDIA_PATHS maps the paths Sonarr/Radarr see to the paths in the bot container, e.g.
"/tv=/media/tv;/movies=/media/movies". Without it the same paths are used.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

CLIP_SECONDS = 20
SPOTS = (0.25, 0.5, 0.72)  # share of the runtime: away from intro and end credits
PRICE_PER_MINUTE = 0.006  # whisper-1, USD
API = "https://api.openai.com/v1/audio/transcriptions"
LANGS = {"german": "German", "english": "English", "french": "French", "spanish": "Spanish",
         "italian": "Italian", "japanese": "Japanese", "dutch": "Dutch", "polish": "Polish",
         "russian": "Russian", "turkish": "Turkish", "korean": "Korean"}


class ListenError(Exception):
    pass


@dataclass
class Track:
    index: int  # n-th audio track
    tag: str  # language tag in the file ("eng", "" ...)
    spoken: str | None = None  # language heard ("German"), None = could not tell (music, silence)


@dataclass
class Heard:
    path: str
    tracks: list[Track] = field(default_factory=list)
    error: str | None = None

    @property
    def german(self) -> bool | None:
        """True: a German track was heard; False: speech heard, none German; None: could not tell."""
        if self.error:
            return None
        spoken = [t.spoken for t in self.tracks if t.spoken]
        if "German" in spoken:
            return True
        return False if spoken else None


def parse_paths(raw: str | None) -> list[tuple[str, str]]:
    out = []
    for part in (raw or "").replace(",", ";").split(";"):
        if "=" in part:
            a, b = part.split("=", 1)
            out.append((a.strip().rstrip("/"), b.strip().rstrip("/")))
    return sorted(out, key=lambda x: -len(x[0]))  # longest prefix first


class Listener:
    def __init__(self, api_key: str | None, paths: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key
        self.paths = parse_paths(paths)
        self.transport = transport
        self.spent = 0.0  # USD since start

    @property
    def ready(self) -> bool:
        return bool(self.api_key) and shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

    def why_not(self) -> str:
        if not self.api_key:
            return "Für „Reinhören“ fehlt ein OpenAI-Key (LISTEN_API_KEY oder KI-Anbieter OpenAI)."
        return "Für „Reinhören“ fehlt ffmpeg im Container – bitte das aktuelle Image bauen."

    def local(self, path: str) -> str:
        for a, b in self.paths:
            if path == a or path.startswith(a + "/"):
                return b + path[len(a):]
        return path

    async def _run(self, *args: str) -> bytes:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 120)
        if proc.returncode:
            raise ListenError(err.decode(errors="replace").strip()[-200:] or "ffmpeg fehlgeschlagen")
        return out

    async def probe(self, path: str) -> tuple[list[Track], float]:
        out = await self._run("ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                              "stream=index:stream_tags=language:format=duration", "-of", "json", path)
        data = json.loads(out or b"{}")
        tracks = [Track(i, ((s.get("tags") or {}).get("language") or "").lower())
                  for i, s in enumerate(data.get("streams") or [])]
        return tracks, float((data.get("format") or {}).get("duration") or 0)

    async def clip(self, path: str, track: int, start: float) -> bytes:
        return await self._run("ffmpeg", "-v", "error", "-ss", f"{start:.0f}", "-i", path, "-map", f"0:a:{track}",
                               "-t", str(CLIP_SECONDS), "-ac", "1", "-ar", "16000", "-b:a", "32k", "-f", "mp3", "pipe:1")

    async def language(self, audio: bytes) -> tuple[str | None, str]:
        """(language heard, text) via OpenAI speech recognition."""
        async with httpx.AsyncClient(timeout=60, transport=self.transport) as http:
            r = await http.post(API, headers={"Authorization": f"Bearer {self.api_key}"},
                                data={"model": "whisper-1", "response_format": "verbose_json"},
                                files={"file": ("clip.mp3", audio, "audio/mpeg")})
        if r.status_code >= 400:
            raise ListenError(f"Spracherkennung: HTTP {r.status_code} {r.text[:120]}")
        self.spent += PRICE_PER_MINUTE * CLIP_SECONDS / 60
        data = r.json()
        text = (data.get("text") or "").strip()
        lang = (data.get("language") or "").lower()
        if len(text.split()) < 4:  # music, silence, a single word: says nothing
            return None, text
        return LANGS.get(lang, lang.title() or None), text

    async def _track_language(self, path: str, track: int, duration: float) -> str | None:
        """Spots spread over the middle of the file - never intro or end credits. Two agreeing
        spots decide (cheap); otherwise the third one breaks the tie. Music/silence does not count."""
        votes: list[str] = []
        for spot in SPOTS:
            lang, _ = await self.language(await self.clip(path, track, duration * spot))
            if lang:
                votes.append(lang)
            if len(votes) >= 2 and votes.count(votes[0]) == len(votes):
                break
        if not votes:
            return None
        best = max(set(votes), key=votes.count)
        return best if votes.count(best) * 2 > len(votes) or len(votes) == 1 else None

    async def hear(self, sonarr_path: str) -> Heard:
        path = self.local(sonarr_path)
        heard = Heard(path)
        if not os.path.exists(path):
            heard.error = f"Datei im Bot-Container nicht gefunden: {path} (MEDIA_PATHS / Ordner einbinden?)"
            return heard
        try:
            heard.tracks, duration = await self.probe(path)
            for t in heard.tracks:
                t.spoken = await self._track_language(path, t.index, duration)
        except (ListenError, asyncio.TimeoutError, httpx.HTTPError, ValueError) as e:
            heard.error = str(e) or type(e).__name__
        return heard


# ---- whole titles -------------------------------------------------------------------------------
MAX_FILES = 80
SAMPLE = 3


def _ep(f: dict) -> str:
    eps = f.get("_eps") or ""
    return eps or (f.get("relativePath") or f.get("path") or "?").rsplit("/", 1)[-1][:40]


async def hear_files(listener: Listener, kind: str, owner_id: int, files: list[dict], title: str,
                     whole_title_suspect: bool) -> tuple[str, list[dict]]:
    """Listen to the given files. German ones are remembered (never flagged again).

    whole_title_suspect: every file looks the same (all tagged wrong?) - then a few spread samples
    decide for the whole title, which keeps it cheap.
    Returns (German text for the user, files that really are not German).
    """
    from .german import confirmed

    files = [f for f in files if f.get("path")][:MAX_FILES]
    if not files:
        return f"🎧 {title}: keine Datei zum Reinhören gefunden.", []
    spent0 = listener.spent
    heard: list[tuple[dict, Heard]] = []
    todo = files
    if whole_title_suspect and len(files) > SAMPLE:
        step = len(files) / SAMPLE
        todo = [files[int(i * step)] for i in range(SAMPLE)]
    for f in todo:
        heard.append((f, await listener.hear(f["path"])))
    if whole_title_suspect and len(todo) < len(files):
        if all(h.german for _, h in heard):
            confirmed.add(kind, owner_id)
            cost = (listener.spent - spent0) * 100
            return (f"🎧 {title}: {len(todo)} Stichproben angehört – überall wird Deutsch gesprochen ✅\n"
                    f"Nur die Tonspur ist falsch markiert. Gemerkt, ich melde das nicht mehr. "
                    f"(Kosten ca. {cost:.1f} ct)".replace(".", ",", 1)), []
        for f in files:  # mixed: every file counts
            if f not in todo:
                heard.append((f, await listener.hear(f["path"])))

    order = {id(f): i for i, f in enumerate(files)}
    heard.sort(key=lambda fh: order[id(fh[0])])
    good = [f for f, h in heard if h.german]
    bad = [(f, h) for f, h in heard if h.german is False]
    unclear = [(f, h) for f, h in heard if h.german is None]
    for f in good:
        confirmed.add_file(kind, f["id"])
    if good and not bad and not unclear and len(heard) == len(files):
        confirmed.add(kind, owner_id)
    cost = (listener.spent - spent0) * 100
    out = [f"🎧 {title}: {len(heard)} {'Datei' if kind[0] == 'm' else 'Folgen'} angehört"]
    if good:
        out.append(f"✅ {len(good)} wirklich Deutsch (nur falsch markiert, gemerkt)")
    if bad:
        langs = sorted({t.spoken for _, h in bad for t in h.tracks if t.spoken})
        out.append(f"🇬🇧 {len(bad)} wirklich nicht Deutsch ({', '.join(langs)}): "
                   + ", ".join(_ep(f) for f, _ in bad[:12]) + (" …" if len(bad) > 12 else ""))
    if unclear:
        why = next((h.error for _, h in unclear if h.error), None) or "nur Musik/Stille gehört"
        out.append(f"❔ {len(unclear)} nicht eindeutig: {why[:160]}")
    out.append(f"(Kosten ca. {cost:.1f} ct)".replace(".", ","))
    return "\n".join(out), [f for f, _ in bad]
