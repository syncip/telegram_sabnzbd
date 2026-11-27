"""📚 Book tools: find an ebook in Chaptarr and send the file into the chat (or as soon as it is downloaded),
in the format the user wants (asked once, converted if needed), and book suggestions with buttons to fetch.

A mixin of ToolRunner (arrbot/tools.py): self is the ToolRunner.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx

from . import booklists, books, toolgroups
from .arr import ArrError
from .tooltypes import Candidate, Final, Flow, Step, View

log = logging.getLogger(__name__)

MAX_HITS = 5
MAX_SUGGEST = 5  # shown
MAX_PICKS = 10  # checked (the AI may send more, some drop out)
RECENT_DAYS = 550  # "aktuell/neu": out in the last ~18 months
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
        said = self.user_text.get(chat_id)
        if fmt and said is not None and not books.mentions_format(said):
            fmt = None  # the AI filled it in by itself: ask the user instead
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

    async def _book_verify(self, pick: dict) -> dict | None:
        """The Chaptarr entry of a suggestion, or None if there is no such book (the AI made it up)."""
        title, author = books._key({"title": pick.get("title"), "authorTitle": pick.get("author") or ""})
        last = author.split()[-1] if author else ""
        try:
            hits = await self.chaptarr.lookup(f"{pick['title']} {pick.get('author') or ''}".strip())
        except ArrError as e:
            log.info("Vorschlag %s: %s", pick.get("title"), e)
            return None
        for h in books.dedupe([h for h in hits if h.get("title")]):
            ht, ha = books._key(h)
            if (title in ht or ht in title) and (not last or last in ha):
                return h
        return None

    async def _t_book_suggest(self, chat_id: int, books_: list | None = None, intro: str = "", recent: bool = False,
                              **kw: Any) -> Final:
        picks = [b for b in (books_ or kw.get("books") or []) if isinstance(b, dict) and b.get("title")][:MAX_PICKS]
        if not picks:
            return Final("🤷 Mir fällt gerade nichts Passendes ein – nenn mir ein Buch oder einen Autor, den du magst.")
        dropped = {"fake": 0, "mine": 0, "old": 0}
        if self.chaptarr is not None:
            found = await asyncio.gather(*(self._book_verify(p) for p in picks))
            mine = {books._key({"title": e.get("title"), "authorTitle": e.get("author")})[0]
                    for e in (self.book_history.of(chat_id) if self.book_history is not None else [])}
            cutoff = (datetime.now() - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
            kept = []
            for p, h in zip(picks, found):
                if h is None:
                    dropped["fake"] += 1
                elif books._key(h)[0] in mine:
                    dropped["mine"] += 1
                elif recent and (h.get("releaseDate") or "")[:10] < cutoff:
                    dropped["old"] += 1
                else:
                    kept.append(dict(p, hit=h))
            picks = kept[:MAX_SUGGEST]
        else:
            picks = picks[:MAX_SUGGEST]
        why_not = ", ".join(t for t, n in (("gibt es so nicht", dropped["fake"]), ("hast du schon", dropped["mine"]),
                                            ("nicht neu genug", dropped["old"])) if n)
        if not picks:
            return Final("🤷 Davon blieb nichts übrig (" + (why_not or "keine Treffer") + "). Sag mir genauer, was du "
                         "suchst – oder ein Buch, das dir gefallen hat.")
        lines = [f"📚 {intro.strip() or 'Das könnte dir gefallen:'}", ""]
        for i, b in enumerate(picks, 1):
            h = b.get("hit") or {}
            year = (h.get("releaseDate") or "")[:4]
            name = books.label(h) if h else f"{b['title']} – {b.get('author') or '?'}"
            if year.isdigit() and f"({year})" not in name:
                name += f" ({year})"
            lines.append(f"{i}. " + ("📗 " if h.get("id") else "") + name + (f"\n   {b['why']}" if b.get("why") else ""))
        if sum(dropped.values()):
            lines.append(f"\n({sum(dropped.values())} weitere habe ich weggelassen: {why_not}.)")
        if self.chaptarr is None:
            return Final("\n".join(lines))
        data = {"suggest": picks, "hits": [], "hit": None, "fmt": self._book_fmt(chat_id)}
        lines.append("\nWelches soll ich dir holen? (📗 = schon da)")
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Buch", None), stage="book", data=data)
        flow.step = Step("\n".join(lines), [(f"📥 {i}. {b['title']}"[:60], f"s{i - 1}") for i, b in enumerate(picks, 1)]
                         + [("✖️ Keins", "no")])
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _t_book_library(self, chat_id: int, query: str | None = None, mine: bool = False,
                              check: bool = False) -> Final:
        """📚 The shelf: the user's books (mine) or all books in Chaptarr, paged, with buttons to send again."""
        if self.chaptarr is None:
            return Final(NOT_SET_UP)
        d = {"hits": [], "hit": None, "fmt": self._book_fmt(chat_id), "page": 0, "query": (query or "").strip(),
             "mode": "mine" if mine else "all", "note": ""}
        if check:
            d["note"] = await self._shelf_rescan(chat_id)
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

    async def _shelf_rescan(self, chat_id: int) -> str:
        """Let Chaptarr rescan its folders, check the files on disk and drop deleted books from the user's list."""
        try:
            await self.chaptarr._req("POST", "/command", json={"name": "RescanFolders"})
        except ArrError as e:
            log.info("Chaptarr RescanFolders: %s", e)
        lib = [b for b in await self.chaptarr.library() if b.get("title")]
        by_id = {b.get("id"): b for b in lib}
        by_foreign = {b.get("foreignBookId"): b for b in lib if b.get("foreignBookId")}
        waiting = {p.book_id for p in (self.books.items if self.books is not None else [])}
        waiting_titles = {p.title for p in (self.books.items if self.books is not None else [])}
        mine = self.book_history.of(chat_id) if self.book_history is not None else []

        async def on_disk(b: dict) -> bool | None:
            """True/False = file there or not; None = cannot tell (bot cannot read the book folder)."""
            try:
                files = await self.chaptarr.files(int(b["id"]))
            except ArrError:
                return None
            return any(os.path.isfile(books.map_path(f.get("path") or "", self.book_paths)) for f in files) \
                if files else False

        found = [by_id.get(e.get("id")) or by_foreign.get(e.get("foreign")) for e in mine]
        disk = await asyncio.gather(*(on_disk(b) if b and books.has_file(b) else asyncio.sleep(0, None)
                                      for b in found))
        trust_disk = any(x is True for x in disk)  # at least one readable file: the mapping works
        keep, gone = [], []
        for e, b, there in zip(mine, found, disk):
            pending = (b and b.get("id") in waiting) or any(e.get("title", "") in t for t in waiting_titles)
            if b and books.has_file(b) and (there is not False or not trust_disk):
                keep.append(e)
            elif pending:
                keep.append(e)  # still on its way
            else:
                gone.append(e.get("title") or "?")
        if self.book_history is not None and gone:
            self.book_history.keep(chat_id, keep)
        if not gone:
            return "🔄 Neu geprüft: alle deine Bücher sind noch da."
        return (f"🔄 Neu geprüft: {len(gone)} nicht mehr da und aus deiner Liste genommen: "
                + ", ".join(gone[:6]) + (" …" if len(gone) > 6 else "") + "."
                + ("" if trust_disk else "\n(Die Dateien selbst konnte ich nicht ansehen – BOOK_PATHS? Siehe /check.)"))

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
        tail = [other, ("🔄 Neu prüfen", "rescan")] + (
            [("🧹 Liste leeren", "reset")] if d["mode"] == "mine" else []) + [("✖️ Schließen", "no")]
        buttons = nums + nav + tail
        rows = [4] * (len(nums) // 4) + ([len(nums) % 4] if len(nums) % 4 else []) + ([len(nav)] if nav else []) + [
            2, len(tail) - 2]
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

    async def _t_book_charts(self, chat_id: int, kind: str = "new", subject: str | None = None,
                             period: str = "weekly") -> list[dict]:
        """Facts for the AI: new releases of a genre in German, or what is trending (Open Library)."""
        try:
            if kind == "trending":
                return await booklists.trending(period)
            return await booklists.new_books(subject)
        except (httpx.HTTPError, ValueError) as e:
            return [{"error": f"Open Library antwortet nicht ({e}) – nimm dein eigenes Wissen, recent=true"}]

    def book_context(self, chat_id: int, text: str) -> str:
        """The user's books (titles only, ~100 tokens), only in front of messages about books."""
        if self.book_history is None or "books" not in toolgroups.groups_for(text):
            return ""
        mine = self.book_history.of(chat_id)[:20]
        if not mine:
            return ""
        return "[Bücher des Nutzers, neueste zuerst: " + "; ".join(
            f"{e.get('title')}" + (f" – {e['author']}" if e.get("author") else "") for e in mine) + "]"

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
            if value == "rescan":
                try:
                    d["note"] = await self._shelf_rescan(flow.chat_id)
                except ArrError as e:
                    d["note"] = f"❌ {e}"
                await self._shelf_load(flow.chat_id, d)
                return self._shelf_step(d)
            if value == "reset":
                return Step("🧹 Deine Bücherliste leeren? Die Bücher bleiben auf dem Server und unter „📚 Alle "
                            "Bücher“; deine Liste füllt sich wieder mit dem, was du ab jetzt anforderst.",
                            [("🧹 Ja, leeren", "reset!"), ("↩️ Zurück", "back")], rows=[2])
            if value == "reset!":
                if self.book_history is not None:
                    self.book_history.keep(flow.chat_id, [])
                d["mode"] = "mine"
                await self._shelf_load(flow.chat_id, d)
                d["note"] = "🧹 Deine Liste ist leer." + (f" {d['note']}" if d.get("note") else "")
                return self._shelf_step(d)
            if value == "back":
                return self._shelf_step(d)
            if value.startswith("n") and value[1:].isdigit() and int(value[1:]) < len(d["shelf"]):
                d["hits"], d["hit"] = [d["shelf"][int(value[1:])]["hit"]], 0
                value = "shelf"
        idx = int(value[1:]) if value[:1] in "bs" and value[1:].isdigit() else -1
        if value == "shelf":
            pass
        elif value.startswith("s") and 0 <= idx < len(d.get("suggest") or []):
            b = d["suggest"][idx]
            hits = [b["hit"]] if b.get("hit") else None
            if hits is None:
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
            d["fmt"], d["asked"] = fmt, True
            if self.book_formats is not None:
                self.book_formats.set(flow.chat_id, fmt)
        elif d.get("again") and value == "no":
            self.flows.pop(flow.id, None)
            return Step(f"👍 Okay, bleibt bei {books.NAMES.get(d.get('was'), 'dem Format')}.", done=True)
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
                d["note"] = await self._book_pick(flow.chat_id, d["hits"][d["hit"]], d["fmt"], False)
            except ArrError as e:
                d["note"] = f"❌ {e}"
            d["hits"], d["hit"] = [], None
            return self._shelf_step(d)
        self.flows.pop(flow.id, None)
        try:
            offer = not d.get("again") and not d.get("asked")
            return Step(await self._book_pick(flow.chat_id, d["hits"][d["hit"]], d["fmt"], offer), done=True)
        except ArrError as e:
            return Step(f"❌ {e}", done=True)

    def _offer_other_format(self, chat_id: int, hit: dict, fmt: str) -> None:
        """Sent in the remembered format without asking: one tap for another one."""
        others = [(t, f"f:{f}") for f, t in books.OFFER if f != fmt]
        data = {"hits": [hit], "hit": 0, "fmt": None, "again": True, "was": fmt}
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Buch", None), stage="book", data=data)
        flow.step = Step(f"🔄 Als {books.NAMES.get(fmt, fmt)} geschickt. Lieber ein anderes Format? "
                         "(Ich merke mir dann das neue.)", others + [("✔️ Passt so", "no")])
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)

    async def _book_pick(self, chat_id: int, hit: dict, fmt: str | None = None, offer: bool = True) -> str:
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
                    if offer and fmt:
                        self._offer_other_format(chat_id, hit, fmt)
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

    async def _book_queue_check(self) -> list[tuple[int, str, list[tuple[str, str]]]]:
        """Downloaded but not imported yet: nudge Chaptarr (it checks only every few minutes by itself), and
        tell the user once if Chaptarr cannot import it (wrong path, wrong format …)."""
        waiting = {p.book_id: p for p in self.books.items if p.book_id}
        if not waiting:
            return []
        try:
            queue = await self.chaptarr.queue()
        except ArrError as e:
            log.info("Chaptarr-Warteschlange: %s", e)
            return []
        out, nudge = [], False
        for q in queue:
            p = waiting.get(q.get("bookId"))
            if p is None:
                continue
            state = (q.get("trackedDownloadState") or "").lower()
            if state in ("importpending", "importing") or (q.get("status") or "").lower() == "completed":
                nudge = True
            if state in ("importblocked", "importfailed", "failedpending") and not p.warned:
                why = "; ".join(m for sm in q.get("statusMessages") or [] for m in sm.get("messages") or [])
                self.books.warn(p)
                out.append((p.chat_id, f"⚠️ „{p.title}“ ist geladen, aber Chaptarr kann es nicht übernehmen"
                                       + (f": {why[:200]}" if why else "") + ". Der Admin sieht in Chaptarr unter "
                                       "Activity → Queue nach; danach schicke ich es dir.", []))
        if nudge:
            try:
                await self.chaptarr._req("POST", "/command", json={"name": "RefreshMonitoredDownloads"})
            except ArrError as e:
                log.info("Chaptarr Import anstoßen: %s", e)
        return out

    async def book_deliveries(self) -> list[tuple[int, str, list[tuple[str, str]]]]:
        """For the job: (chat, text, files) of requested books that arrived or gave up."""
        out: list[tuple[int, str, list[tuple[str, str]]]] = []
        if self.chaptarr is None or self.books is None or not self.books.items:
            return out
        out += await self._book_queue_check()
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
