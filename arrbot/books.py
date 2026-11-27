"""📚 Books in the chat: "Schick mir das Buch Der Schwarm" → the ebook file comes as a Telegram document.

Already in Chaptarr with a file: sent right away. Otherwise Chaptarr adds and searches it, the request waits
in data/books.json and a job sends the file as soon as it is imported (or says after WAIT_DAYS that nothing
was found). Telegram lets bots send files up to 50 MB.

Each user picks a format once (EPUB, PDF, Kindle; data/book_formats.json). Is the book not there in that
format, Calibre's ebook-convert (in the Docker image) converts it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

FORMATS = ["epub", "azw3", "mobi", "kfx", "pdf", "cbz", "cbr", "txt"]  # preferred first
MAX_MB = 50
MAX_FILES = 2
WAIT_DAYS = 7
CONVERT_SECONDS = 300
# The formats a user can choose (button text)
OFFER = [("epub", "📱 EPUB – Handy, Tolino, Kobo, Apple Books"), ("pdf", "📄 PDF"),
         ("azw3", "📖 Kindle (AZW3)")]
NAMES = {"epub": "EPUB", "pdf": "PDF", "azw3": "Kindle (AZW3)", "mobi": "MOBI"}
ALIASES = {"epub": "epub", "pdf": "pdf", "azw3": "azw3", "kindle": "azw3", "mobi": "mobi", "azw": "azw3"}


def norm_format(text: str | None) -> str | None:
    t = (text or "").strip().lower().lstrip(".")
    return ALIASES.get(t) or next((v for k, v in ALIASES.items() if k in t), None) if t else None


def ext_of(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""


def label(hit: dict) -> str:
    author = (hit.get("author") or {}).get("authorName") or hit.get("authorTitle") or ""
    year = (hit.get("releaseDate") or "")[:4]
    out = hit.get("title") or "?"
    if author:
        out += f" – {author}"
    return out + (f" ({year})" if year.isdigit() else "")


def _key(hit: dict) -> tuple[str, str]:
    def norm(s: str) -> str:
        return " ".join("".join(c if c.isalnum() else " " for c in s.lower()).split())
    author = (hit.get("author") or {}).get("authorName") or hit.get("authorTitle") or ""
    return norm(hit.get("title") or ""), norm(author)


def dedupe(hits: list[dict]) -> list[dict]:
    """One entry per book: Chaptarr lists each edition/printing on its own. Keeps the one in the library,
    else the newest; the order of first appearance stays."""
    best: dict[tuple[str, str], dict] = {}
    for h in hits:
        k = _key(h)
        old = best.get(k)
        if old is None or (h.get("id") and not old.get("id")) or (
                bool(h.get("id")) == bool(old.get("id")) and (h.get("releaseDate") or "") > (old.get("releaseDate") or "")):
            best[k] = h
    seen, out = set(), []
    for h in hits:
        k = _key(h)
        if k not in seen:
            seen.add(k)
            out.append(best[k])
    return out


def button(i: int, hit: dict) -> str:
    """Short button text; the full title is in the message (Telegram cuts long buttons)."""
    title = hit.get("title") or "?"
    year = (hit.get("releaseDate") or "")[:4]
    short = title if len(title) <= 34 else title[:33].rstrip(" ,:-–") + "…"
    return ("📗 " if hit.get("id") else "") + f"{i + 1}. {short}" + (f" ({year})" if year.isdigit() else "")


def numbered(hits: list[dict]) -> list[str]:
    out = []
    for i, h in enumerate(hits):
        pages = h.get("pageCount")
        out.append(f"{i + 1}. " + ("📗 " if h.get("id") else "") + label(h) + (f", {pages} S." if pages else ""))
    return out


def map_path(path: str, mapping: str | None) -> str:
    """Chaptarr's path → the bot's path. mapping: "/books=/media/books;/data=/mnt/data"."""
    for pair in (mapping or "").replace(",", ";").split(";"):
        if "=" not in pair:
            continue
        src, dst = (x.strip().rstrip("/") for x in pair.split("=", 1))
        if src and (path == src or path.startswith(src + "/")):
            return dst + path[len(src):]
    return path


def pick_files(files: list[dict], mapping: str | None, max_mb: int = MAX_MB) -> tuple[list[tuple[str, str]], list[str]]:
    """(local path, file name) to send, best format first, and the problems (too big, not readable)."""
    def rank(f: dict) -> int:
        ext = (f.get("path") or "").rsplit(".", 1)[-1].lower()
        return FORMATS.index(ext) if ext in FORMATS else len(FORMATS)

    send, problems = [], []
    for f in sorted(files, key=rank):
        if len(send) >= MAX_FILES:
            break
        path = f.get("path") or ""
        local = map_path(path, mapping)
        name = path.replace("\\", "/").rsplit("/", 1)[-1] or "buch"
        if not os.path.isfile(local):
            problems.append(f"{name}: Datei nicht lesbar ({local}) – ist der Bücher-Ordner im Bot eingebunden (BOOK_PATHS)?")
            continue
        size = os.path.getsize(local)
        if size > max_mb * 2**20:
            problems.append(f"{name}: {size / 2**20:.0f} MB – Telegram erlaubt Bots nur {max_mb} MB")
            continue
        if any(n == name for _, n in send):
            continue
        send.append((local, name))
    return send, problems


def converter() -> str | None:
    return shutil.which("ebook-convert")


async def convert(src: str, fmt: str, cache_dir: str) -> str:
    """src → cache_dir/<name>.<fmt> with Calibre. Raises RuntimeError with a short reason."""
    tool = converter()
    if not tool:
        raise RuntimeError("Calibre (ebook-convert) fehlt im Container")
    os.makedirs(cache_dir, exist_ok=True)
    old = time.time() - 86400
    for f in os.scandir(cache_dir):  # yesterday's conversions: already sent
        if f.is_file() and f.stat().st_mtime < old:
            os.remove(f.path)
    stem = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(cache_dir, f"{stem}.{fmt}")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox")
    proc = await asyncio.create_subprocess_exec(tool, src, out, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE, env=env)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), CONVERT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"dauerte über {CONVERT_SECONDS // 60} Minuten") from None
    if proc.returncode != 0 or not os.path.isfile(out):
        tail = (err or b"").decode(errors="replace").strip().splitlines()[-1:] or ["unbekannter Fehler"]
        raise RuntimeError(tail[0][:150])
    return out


async def prepare(files: list[dict], mapping: str | None, fmt: str | None, cache_dir: str,
                  max_mb: int = MAX_MB) -> tuple[list[tuple[str, str]], list[str]]:
    """The one file to send in the wanted format (converted if needed), and what went wrong."""
    send, problems = pick_files(files, mapping, max_mb)
    if not fmt:
        return send[:1], problems
    exact = [x for x in send if ext_of(x[1]) == fmt]
    if exact:
        return exact[:1], []
    readable = [(map_path(f.get("path") or "", mapping), f) for f in files]
    readable = [(p, f) for p, f in readable if os.path.isfile(p)]
    if not readable:
        return [], problems
    src = min(readable, key=lambda x: FORMATS.index(ext_of(x[0])) if ext_of(x[0]) in FORMATS else 99)[0]
    try:
        out = await convert(src, fmt, cache_dir)
    except (RuntimeError, OSError) as e:
        log.warning("Umwandeln %s → %s: %s", src, fmt, e)
        problems = [f"In {NAMES.get(fmt, fmt)} umwandeln ging nicht ({e})" + (" – hier im Original." if send else ".")]
        return send[:1], problems
    size = os.path.getsize(out)
    if size > max_mb * 2**20:
        return send[:1], [f"Als {NAMES.get(fmt, fmt)} wären es {size / 2**20:.0f} MB – Telegram erlaubt Bots nur "
                          f"{max_mb} MB" + (" – hier im Original." if send else ".")]
    return [(out, os.path.basename(out))], []


class BookHistory:
    """The books each chat asked for or got (data/book_history.json), newest first – for /buecher."""

    MAX = 300

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "book_history.json"
        self._lock = threading.Lock()
        try:
            self.by_chat: dict[str, list[dict]] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.by_chat = {}

    def add(self, chat_id: int, hit: dict, book_id: int | None = None) -> None:
        entry = {"id": book_id or hit.get("id") or 0, "title": hit.get("title") or "?",
                 "author": (hit.get("author") or {}).get("authorName") or hit.get("authorTitle") or "",
                 "foreign": hit.get("foreignBookId") or "", "at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            old = [e for e in self.by_chat.get(str(chat_id), [])
                   if not ((entry["id"] and e.get("id") == entry["id"]) or
                           (entry["foreign"] and e.get("foreign") == entry["foreign"]) or
                           (e.get("title"), e.get("author")) == (entry["title"], entry["author"]))]
            self.by_chat[str(chat_id)] = ([entry] + old)[:self.MAX]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.by_chat, ensure_ascii=False), encoding="utf-8")

    def of(self, chat_id: int) -> list[dict]:
        return list(self.by_chat.get(str(chat_id), []))


def has_file(b: dict) -> bool:
    return bool(b.get("hasFiles") or (b.get("statistics") or {}).get("bookFileCount"))


class BookFormats:
    """Which format each chat wants (data/book_formats.json)."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "book_formats.json"
        try:
            self.by_chat: dict[str, str] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.by_chat = {}

    def get(self, chat_id: int) -> str | None:
        return self.by_chat.get(str(chat_id))

    def set(self, chat_id: int, fmt: str) -> None:
        self.by_chat[str(chat_id)] = fmt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.by_chat), encoding="utf-8")


@dataclass
class Pending:
    book_id: int
    chat_id: int
    title: str
    at: str
    fmt: str = ""
    ref: dict = field(default_factory=dict)  # book_id 0 = not in Chaptarr yet: title/titleSlug/foreignBookId

    @property
    def ident(self) -> tuple:
        return (self.book_id or self.ref.get("foreignBookId") or self.title, self.chat_id)


class PendingBooks:
    """Requested books that are still downloading (data/books.json, survives restarts)."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "books.json"
        self._lock = threading.Lock()
        try:
            self.items = [Pending(**x) for x in json.loads(self.path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            self.items = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(x) for x in self.items], ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, book_id: int, chat_id: int, title: str, fmt: str = "", ref: dict | None = None) -> None:
        with self._lock:
            new = Pending(book_id, chat_id, title, datetime.now(timezone.utc).isoformat(), fmt, dict(ref or {}))
            if not any(x.ident == new.ident for x in self.items):
                self.items.append(new)
                self._save()

    def remove(self, p: Pending) -> None:
        with self._lock:
            self.items = [x for x in self.items if x.ident != p.ident]
            self._save()

    def found(self, p: Pending, book_id: int) -> None:
        """A request that waited for Chaptarr's metadata is in the library now."""
        with self._lock:
            p.book_id = book_id
            self._save()

    def expired(self, p: Pending, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        try:
            return datetime.fromisoformat(p.at) < now - timedelta(days=WAIT_DAYS)
        except ValueError:
            return True
