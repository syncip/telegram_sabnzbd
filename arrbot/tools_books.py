"""📚 Book tool: find an ebook in Chaptarr and send the file into the chat (or as soon as it is downloaded).

A mixin of ToolRunner (arrbot/tools.py): self is the ToolRunner.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from . import books
from .arr import ArrError
from .tooltypes import Candidate, Final, Flow, Step, View

log = logging.getLogger(__name__)

MAX_HITS = 5


class BookTools:
    chaptarr: Any = None  # chaptarr.Chaptarr, set in bot.py when CHAPTARR_URL is configured
    books: Any = None  # books.PendingBooks
    book_paths: str | None = None  # "chaptarr path=bot path;…"

    async def _t_book(self, chat_id: int, title: str, author: str | None = None) -> Final:
        if self.chaptarr is None:
            return Final("📚 Bücher sind nicht eingerichtet: Der Admin trägt CHAPTARR_URL und CHAPTARR_API_KEY "
                         "in der docker-compose.yml ein.")
        term = f"{title} {author}".strip() if author else title
        hits = [h for h in await self.chaptarr.lookup(term) if h.get("title")][:MAX_HITS]
        if not hits:
            return Final(f"🤷 Zu „{term}“ finde ich kein Buch. Stimmt der Titel? Mit Autor klappt es oft besser.")
        if len(hits) == 1:
            return Final(await self._book_pick(chat_id, hits[0]))
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Buch", None), stage="book", data=hits)
        flow.step = Step("📚 Welches Buch meinst du?",
                         [(("📗 " if h.get("id") else "") + books.label(h)[:60], f"b{i}") for i, h in enumerate(hits)]
                         + [("✖️ Keins davon", "no")])
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _book_step(self, flow: Flow, value: str) -> Step:
        self.flows.pop(flow.id, None)
        if value == "no" or not (value.startswith("b") and value[1:].isdigit() and int(value[1:]) < len(flow.data)):
            return Step("👍 Okay, dann nicht. Schreib mir den Titel gern genauer (mit Autor).", done=True)
        try:
            return Step(await self._book_pick(flow.chat_id, flow.data[int(value[1:])]), done=True)
        except ArrError as e:
            return Step(f"❌ {e}", done=True)

    async def _book_pick(self, chat_id: int, hit: dict) -> str:
        """Send it if Chaptarr has the file, else add/search it and remember the request."""
        name = books.label(hit)
        book_id = int(hit["id"]) if hit.get("id") else None
        if book_id:
            files = await self.chaptarr.files(book_id)
            if files:
                send, problems = books.pick_files(files, self.book_paths)
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
            self.books.add(book_id, chat_id, name)
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
                send, problems = books.pick_files(files, self.book_paths)
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
