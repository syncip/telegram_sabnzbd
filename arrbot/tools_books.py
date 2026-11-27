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
PAGE = 8  # books per page in /buecher
STATE = {"have": "📗", "search": "🔎", "prep": "⏳"}
STATE_TEXT = {"search": " – wird gesucht", "prep": " – Chaptarr bereitet es vor"}
NOT_SET_UP = ("📚 Bücher sind nicht eingerichtet: Der Admin trägt CHAPTARR_URL und CHAPTARR_API_KEY "
              "in der docker-compose.yml ein.")


class BookTools:
    chaptarr: Any = None  # chaptarr.Chaptarr, set in bot.py when CHAPTARR_URL is configured
    books: Any = None  # books.PendingBooks
    book_formats: Any = None  # books.BookFormats
    book_paths: str | None = None  # "chaptarr path=bot path;…"
    book_cache: str | None = None  # where converted files go (data/book_cache)
    book_history: Any = None  # books.BookHistory

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
        hits = books.dedupe([h for h in await self.chaptarr.lookup(term) if h.get("title")])[:MAX_HITS]
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

    async def _t_book_library(self, chat_id: int, query: str | None = None, mine: bool = False) -> Final:
        """📚 The shelf: the user's books (mine) or all books in Chaptarr, paged, with buttons to send again."""
        if self.chaptarr is None:
            return Final(NOT_SET_UP)
        d = {"hits": [], "hit": None, "fmt": self._book_fmt(chat_id), "page": 0, "query": (query or "").strip(),
             "mode": "mine" if mine else "all", "note": ""}
        await self._shelf_load(chat_id, d)
        if not d["shelf"]:
            q = d["query"]
            return Final(f"📚 Zu „{q}“ habe ich keine Bücher." if q else "📚 Noch keine Bücher in Chaptarr.")
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Buch", None), stage="book", data=d)
        flow.step = self._shelf_step(d)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _shelf_load(self, chat_id: int, d: dict) -> None:
        lib = [b for b in await self.chaptarr.library() if b.get("title")]
        entries: list[dict] = []
        mine = self.book_history.of(chat_id) if self.book_history is not None else []
        if d["mode"] == "mine" and not mine:
            d["mode"], d["note"] = "all", "Von dir habe ich noch keine Bücher – hier alles auf dem Server."
        if d["mode"] == "mine":
            by_id = {b.get("id"): b for b in lib}
            by_foreign = {b.get("foreignBookId"): b for b in lib if b.get("foreignBookId")}
            for e in mine:
                b = by_id.get(e.get("id")) or by_foreign.get(e.get("foreign"))
                hit = b or {"title": e.get("title"), "authorTitle": e.get("author")}
                state = "have" if b and books.has_file(b) else ("search" if b else "prep")
                entries.append({"hit": hit, "state": state})
        else:
            have = sorted((b for b in lib if books.has_file(b)), key=lambda b: b.get("added") or "", reverse=True)
            wanted = [b for b in lib if not books.has_file(b) and (b.get("monitored") or b.get("ebookMonitored"))]
            entries = [{"hit": b, "state": "have"} for b in have] + [{"hit": b, "state": "search"} for b in wanted]
        q = d["query"].lower()
        if q:
            entries = [e for e in entries if q in books.label(e["hit"]).lower()]
        d["shelf"] = entries
        d["page"] = 0

    def _shelf_step(self, d: dict) -> Step:
        shelf, n = d["shelf"], len(d["shelf"])
        pages = max(1, -(-n // PAGE))
        page = d["page"] = min(max(d["page"], 0), pages - 1)
        head = ("📚 Deine Bücher" if d["mode"] == "mine" else "📚 Alle Bücher auf dem Server") + f" ({n})"
        if d["query"]:
            head += f" zu „{d['query']}“"
        if pages > 1:
            head += f" · Seite {page + 1}/{pages}"
        lines = ([d["note"], ""] if d.get("note") else []) + [head, ""]
        start = page * PAGE
        nums: list[tuple[str, str]] = []
        for i, e in enumerate(shelf[start:start + PAGE], start):
            lines.append(f"{i + 1}. {STATE[e['state']]} {books.label(e['hit'])}{STATE_TEXT.get(e['state'], '')}")
            if e["state"] == "have":
                nums.append((f"📥 {i + 1}", f"n{i}"))
        fmt = d.get("fmt")
        lines.append("")
        if nums:
            lines.append("Tippe 📥 + Nummer, dann schicke ich es dir" +
                         (f" als {books.NAMES.get(fmt, fmt)}." if fmt else " (das Format frage ich einmal ab)."))
        else:
            lines.append("Auf dieser Seite ist noch nichts zum Schicken da.")
        nav = ([("◀️ Zurück", f"pg:{page - 1}")] if page > 0 else []) + (
            [("Weiter ▶️", f"pg:{page + 1}")] if page < pages - 1 else [])
        other = ("📚 Alle Bücher", "all") if d["mode"] == "mine" else ("👤 Meine Bücher", "mine")
        buttons = nums + nav + [other, ("✖️ Schließen", "no")]
        rows = [4] * (len(nums) // 4) + ([len(nums) % 4] if len(nums) % 4 else []) + ([len(nav)] if nav else []) + [2]
        return Step("\n".join(lines), buttons, rows=rows)

    async def book_access(self) -> list[str]:
        """For /check: can the bot read the book files, and can it convert?"""
        if self.chaptarr is None:
            return []
        out = ["🔄 Umwandeln (Calibre): " + ("da ✅" if books.converter() else "fehlt ❌ – Bücher kommen nur im Original")]
        try:
            lib = [b for b in await self.chaptarr.library() if books.has_file(b)]
            files = await self.chaptarr.files(int(lib[0]["id"])) if lib else []
        except ArrError as e:
            return [f"📚 Bücher-Ordner: Chaptarr antwortet nicht ({e})"] + out
        if not files:
            return ["📚 Bücher-Ordner: noch kein Buch mit Datei zum Prüfen"] + out
        path = files[0].get("path") or ""
        local = books.map_path(path, self.book_paths)
        if os.path.isfile(local):
            return [f"📚 Bücher-Ordner: lesbar ✅ ({local})"] + out
        folder = path.rsplit("/", 2)[0] if path.count("/") > 2 else path
        return [f"📚 Bücher-Ordner: ❌ nicht lesbar. Chaptarr sagt {path}, der Bot findet {local} nicht. "
                f"Bücher-Ordner im Bot einbinden und BOOK_PATHS setzen, z. B. \"{folder}=/books\"."] + out

    def _book_question(self, data: dict) -> Step:
        hits = data["hits"]
        if data["hit"] is None:
            return Step("\n".join(["📚 Welches Buch meinst du? (📗 = schon da)", ""] + books.numbered(hits)),
                        [(books.button(i, h), f"b{i}") for i, h in enumerate(hits)] + [("✖️ Keins davon", "no")])
        return Step(f"📚 „{books.label(hits[data['hit']])}“ – in welchem Format willst du es?\n"
                    "Ich merke es mir für die nächsten Bücher (ändern: „Bücher als PDF“).",
                    [(text, f"f:{fmt}") for fmt, text in books.OFFER] + [("✖️ Doch nicht", "no")])

    async def _book_step(self, flow: Flow, value: str) -> Step:
        d = flow.data
        if d.get("shelf") is not None:
            if value.startswith("pg:") and value[3:].lstrip("-").isdigit():
                d["page"] = int(value[3:])
                return self._shelf_step(d)
            if value in ("all", "mine"):
                d["mode"], d["note"] = value, ""
                await self._shelf_load(flow.chat_id, d)
                return self._shelf_step(d)
            if value.startswith("n") and value[1:].isdigit() and int(value[1:]) < len(d["shelf"]):
                d["hits"], d["hit"] = [d["shelf"][int(value[1:])]["hit"]], 0
                value = "shelf"
        idx = int(value[1:]) if value[:1] in "bs" and value[1:].isdigit() else -1
        if value == "shelf":
            pass
        elif value.startswith("s") and 0 <= idx < len(d.get("suggest") or []):
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
        elif d.get("shelf") is not None and value == "no" and d["hit"] is not None:
            d["hits"], d["hit"], d["note"] = [], None, ""
            return self._shelf_step(d)
        else:
            self.flows.pop(flow.id, None)
            if d.get("shelf") is not None:
                return Step("📚 Bücherliste geschlossen. /buecher öffnet sie wieder.", done=True)
            return Step("👍 Okay, dann nicht. Schreib mir den Titel gern genauer (mit Autor).", done=True)
        if d["hit"] is None or not d["fmt"]:
            return self._book_question(d)
        if d.get("shelf") is not None:  # from the shelf: send it and show the shelf again for the next one
            try:
                d["note"] = await self._book_pick(flow.chat_id, d["hits"][d["hit"]], d["fmt"])
            except ArrError as e:
                d["note"] = f"❌ {e}"
            d["hits"], d["hit"] = [], None
            return self._shelf_step(d)
        self.flows.pop(flow.id, None)
        try:
            return Step(await self._book_pick(flow.chat_id, d["hits"][d["hit"]], d["fmt"]), done=True)
        except ArrError as e:
            return Step(f"❌ {e}", done=True)

    async def _book_pick(self, chat_id: int, hit: dict, fmt: str | None = None) -> str:
        """Send it if Chaptarr has the file, else add/search it and remember the request."""
        name = books.label(hit)
        book_id = int(hit["id"]) if hit.get("id") else None
        if self.book_history is not None:
            self.book_history.add(chat_id, hit)
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
                if self.books is not None:
                    ref = {k: hit.get(k) for k in ("title", "titleSlug", "foreignBookId") if hit.get(k)}
                    self.books.add(0, chat_id, name, fmt or "", ref)
                return (f"⏳ „{name}“ bereitet Chaptarr gerade noch vor (die Buchdaten werden erst geladen). "
                        "Ich bleibe dran: sobald es drin ist, wird gesucht und ich schicke es dir.")
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
                if not p.book_id:  # waited for Chaptarr's metadata: is it in the library now?
                    if book_id := await self.chaptarr.find(p.ref):
                        await self.chaptarr.want(book_id)
                        self.books.found(p, book_id)
                    elif self.books.expired(p):
                        self.books.remove(p)
                        out.append((p.chat_id, f"🤷 „{p.title}“ hat Chaptarr in {books.WAIT_DAYS} Tagen nicht "
                                               "hinzugefügt. Frag mich später nochmal danach.", []))
                    continue
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
