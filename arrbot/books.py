"""📚 Books in the chat: "Schick mir das Buch Der Schwarm" → the ebook file comes as a Telegram document.

Already in Chaptarr with a file: sent right away. Otherwise Chaptarr adds and searches it, the request waits
in data/books.json and a job sends the file as soon as it is imported (or says after WAIT_DAYS that nothing
was found). Telegram lets bots send files up to 50 MB.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

FORMATS = ["epub", "azw3", "mobi", "kfx", "pdf", "cbz", "cbr", "txt"]  # preferred first
MAX_MB = 50
MAX_FILES = 2
WAIT_DAYS = 7


def label(hit: dict) -> str:
    author = (hit.get("author") or {}).get("authorName") or hit.get("authorTitle") or ""
    year = (hit.get("releaseDate") or "")[:4]
    out = hit.get("title") or "?"
    if author:
        out += f" – {author}"
    return out + (f" ({year})" if year.isdigit() else "")


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


@dataclass
class Pending:
    book_id: int
    chat_id: int
    title: str
    at: str


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

    def add(self, book_id: int, chat_id: int, title: str) -> None:
        with self._lock:
            if not any(x.book_id == book_id and x.chat_id == chat_id for x in self.items):
                self.items.append(Pending(book_id, chat_id, title, datetime.now(timezone.utc).isoformat()))
                self._save()

    def remove(self, p: Pending) -> None:
        with self._lock:
            self.items = [x for x in self.items if not (x.book_id == p.book_id and x.chat_id == p.chat_id)]
            self._save()

    def expired(self, p: Pending, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        try:
            return datetime.fromisoformat(p.at) < now - timedelta(days=WAIT_DAYS)
        except ValueError:
            return True
