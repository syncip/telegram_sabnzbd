"""📚 Book tools: find an ebook in Chaptarr and send the file into the chat (or as soon as it is downloaded),
in the format the user wants (asked once, converted if needed), and book suggestions with buttons to fetch.

A mixin of ToolRunner (arrbot/tools.py): self is the ToolRunner.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from typing import Any

from . import books
from .arr import ArrError
from .tooltypes import Candidate, Final, Flow, Step, View

log = logging.getLogger(__name__)

MAX_HITS = 5
MAX_SUGGEST = 6
LIST = 30  # books listed in "welche Bücher haben wir?"
NOT_SET_UP = ("📚 Bücher sind nicht eingerichtet: Der Admin trägt CHAPTARR_URL und CHAPTARR_API_KEY "
              "in der docker-compose.yml ein.")


class BookTools:
    chaptarr: Any = None  # chaptarr.Chaptarr, set in bot.py when CHAPTARR_URL is configured
    books: Any = None  # books.PendingBooks
    book_formats: Any = None  # books.BookFormats
    book_paths: str | None = None  # "chaptarr path=bot path;…"
    book_cache: str | None = None  # where converted files go (data/book_cache)

    def _book_fmt(self, chat_id: int) -> str | None:
        return self.book_formats.get(chat_id) if self.book_formats is not None else None

    def _cache_dir(self) -> str:
        return self.book_cache or os.path.join(tempfile.gettempdir(), "arrbot_books")

    def _book_flow(self, chat_id: int, data: dict) -> Final:
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Buch", None), stage="book", data=data)
        flow.step = self._book_question(data)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _t_book(self, chat_id: int, title: str, author: str | None = None, format: str | None = None) -> Final:
        if self.chaptarr is None:
            return Final(NOT_SET_UP)
        fmt = books.norm_format(format)
        if fmt and self.book_formats is not None:
            self.book_formats.set(chat_id, fmt)  # "als PDF" = from now on PDF
        term = f"{title} {author}".strip() if author else title
        hits = [h for h in await self.chaptarr.lookup(term) if h.get("title")][:MAX_HITS]
        if not hits:
            return Final(f"🤷 Zu „{term}“ finde ich kein Buch. Stimmt der Titel? Mit Autor klappt es oft besser.")
        data = {"hits": hits, "hit": 0 if len(hits) == 1 else None, "fmt": fmt or self._book_fmt(chat_id)}
        if data["hit"] is not None and data["fmt"]:
            return Final(await self._book_pick(chat_id, hits[0], data["fmt"]))
        return self._book_flow(chat_id, data)

    async def _t_book_suggest(self, chat_id: int, books_: list | None = None, intro: str = "", **kw: Any) -> Final:
        picks = [b for b in (books_ or kw.get("books") or []) if isinstance(b, dict) and b.get("title")][:MAX_SUGGEST]
        if not picks:
            return Final("🤷 Mir fällt gerade nichts Passendes ein – nenn mir ein Buch oder einen Autor, den du magst.")
        lines = [f"📚 {intro.strip() or 'Das könnte dir gefallen:'}", ""]
        for i, b in enumerate(picks, 1):
            lines.append(f"{i}. {b['title']} – {b.get('author') or '?'}" + (f"\n   {b['why']}" if b.get("why") else ""))
        if self.chaptarr is None:
            return Final("\n".join(lines))
        data = {"suggest": picks, "hits": [], "hit": None, "fmt": self._book_fmt(chat_id)}
        lines.append("\nWelches soll ich dir holen?")
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Buch", None), stage="book", data=data)
        flow.step = Step("\n".join(lines), [(f"📥 {i}. {b['title']}"[:60], f"s{i - 1}") for i, b in enumerate(picks, 1)]
                         + [("✖️ Keins", "no")])
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _t_book_library(self, chat_id: int, query: str | None = None) -> Final:
        if self.chaptarr is None:
            return Final(NOT_SET_UP)
        q = (query or "").strip().lower()
        every = [b for b in await self.chaptarr.library() if b.get("title")]
        if q:
            every = [b for b in every if q in books.label(b).lower()]
        def has_file(b: dict) -> bool:
            return bool(b.get("hasFiles") or (b.get("statistics") or {}).get("bookFileCount"))
        have = sorted((b for b in every if has_file(b)), key=lambda b: b.get("added") or "", reverse=True)
        wanted = [b for b in every if not has_file(b) and (b.get("monitored") or b.get("ebookMonitored"))]
        if not have and not wanted:
            return Final(f"📚 Zu „{query}“ habe ich keine Bücher." if q else "📚 Noch keine Bücher in Chaptarr.")
        lines = [f"📚 Da sind {len(have)} Bücher" + (f" zu „{query}“" if q else "") + (" (neueste zuerst):" if have else ".")]
        lines += [f"📗 {books.label(b)}" for b in have[:LIST]]
        if len(have) > LIST:
            lines.append(f"… und {len(have) - LIST} weitere – frag nach Titel oder Autor.")
        if wanted:
            lines.append(f"\n🔎 Noch gesucht ({len(wanted)}): " + ", ".join(b["title"] for b in wanted[:8])
                         + (" …" if len(wanted) > 8 else ""))
        if not have:
            return Final("\n".join(lines))
        lines.append("\nTippe eins an, dann schicke ich es dir:")
        data = {"hits": have[:MAX_HITS * 2], "hit": None, "fmt": self._book_fmt(chat_id)}
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Buch", None), stage="book", data=data)
        flow.step = Step("\n".join(lines), self._book_question(data).buttons)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    def _book_question(self, data: dict) -> Step:
        hits = data["hits"]
        if data["hit"] is None:
            return Step("📚 Welches Buch meinst du?",
                        [(("📗 " if h.get("id") else "") + books.label(h)[:60], f"b{i}") for i, h in enumerate(hits)]
                        + [("✖️ Keins davon", "no")])
        return Step(f"📚 „{books.label(hits[data['hit']])}“ – in welchem Format willst du es?\n"
                    "Ich merke es mir für die nächsten Bücher (ändern: „Bücher als PDF“).",
                    [(text, f"f:{fmt}") for fmt, text in books.OFFER] + [("✖️ Doch nicht", "no")])

    async def _book_step(self, flow: Flow, value: str) -> Step:
        d = flow.data
        idx = int(value[1:]) if value[:1] in "bs" and value[1:].isdigit() else -1
        if value.startswith("s") and 0 <= idx < len(d.get("suggest") or []):
            b = d["suggest"][idx]
            try:
                hits = [h for h in await self.chaptarr.lookup(f"{b['title']} {b.get('author') or ''}".strip())
                        if h.get("title")]
            except ArrError as e:
                self.flows.pop(flow.id, None)
                return Step(f"❌ {e}", done=True)
            if not hits:
                return Step(f"🤷 „{b['title']}“ finde ich in Chaptarr nicht. Nimm ein anderes:", flow.step.buttons)
            d["hits"], d["hit"] = hits[:1], 0  # the best match: no edition question for a suggestion
        elif value.startswith("b") and 0 <= idx < len(d["hits"]):
            d["hit"] = idx
        elif value.startswith("f:") and (fmt := books.norm_format(value[2:])):
            d["fmt"] = fmt
            if self.book_formats is not None:
                self.book_formats.set(flow.chat_id, fmt)
        else:
            self.flows.pop(flow.id, None)
            return Step("👍 Okay, dann nicht. Schreib mir den Titel gern genauer (mit Autor).", done=True)
        if d["hit"] is None or not d["fmt"]:
            return self._book_question(d)
        self.flows.pop(flow.id, None)
        try:
            return Step(await self._book_pick(flow.chat_id, d["hits"][d["hit"]], d["fmt"]), done=True)
        except ArrError as e:
            return Step(f"❌ {e}", done=True)

    async def _book_pick(self, chat_id: int, hit: dict, fmt: str | None = None) -> str:
        """Send it if Chaptarr has the file, else add/search it and remember the request."""
        name = books.label(hit)
        book_id = int(hit["id"]) if hit.get("id") else None
        if book_id:
            files = await self.chaptarr.files(book_id)
            if files:
                send, problems = await books.prepare(files, self.book_paths, fmt, self._cache_dir())
                if send:
                    self._outbox.setdefault(chat_id, []).append(View(chat_id, "file", arg=send))
                    return f"📚 Hier ist „{name}“." + (f"\n⚠️ {problems[0]}" if problems else "")
                return f"⚠️ „{name}“ ist da, aber ich kann es nicht schicken: {problems[0]}"
        else:
            book_id = await self.chaptarr.add(hit)
            if book_id is None:
                return (f"⏳ Chaptarr lädt gerade noch die Daten zu „{name}“. Frag mich in ein paar Minuten "
                        "nochmal danach.")
        await self.chaptarr.want(book_id)
        if self.books is not None:
            self.books.add(book_id, chat_id, name, fmt or "")
        await self._added(chat_id, f"📚 {name}")
        return f"🔎 „{name}“ habe ich noch nicht – ich suche es und schicke es dir, sobald es da ist."

    async def book_deliveries(self) -> list[tuple[int, str, list[tuple[str, str]]]]:
        """For the job: (chat, text, files) of requested books that arrived or gave up."""
        out: list[tuple[int, str, list[tuple[str, str]]]] = []
        if self.chaptarr is None or self.books is None:
            return out
        for p in list(self.books.items):
            try:
                files = await self.chaptarr.files(p.book_id)
            except ArrError as e:
                log.info("Buch %s: %s", p.title, e)
                continue
            if files:
                send, problems = await books.prepare(files, self.book_paths, p.fmt or self._book_fmt(p.chat_id),
                                                     self._cache_dir())
                self.books.remove(p)
                if send:
                    out.append((p.chat_id, f"📚 „{p.title}“ ist da:" + (f"\n⚠️ {problems[0]}" if problems else ""), send))
                else:
                    out.append((p.chat_id, f"⚠️ „{p.title}“ ist da, aber ich kann es nicht schicken: {problems[0]}", []))
            elif self.books.expired(p):
                self.books.remove(p)
                out.append((p.chat_id, f"🤷 „{p.title}“ habe ich in {books.WAIT_DAYS} Tagen nicht gefunden. "
                                       "Chaptarr sucht weiter; frag mich später nochmal.", []))
        return out
