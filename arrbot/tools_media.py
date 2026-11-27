"""Media manager, duplicates, storage, keep, watched questions, cleanup, Plex admin.

A mixin of ToolRunner (arrbot/tools.py): self is the ToolRunner."""

from __future__ import annotations

import asyncio
from builtins import max as builtins_max
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any
import uuid

from . import collections_plex, dupes, mediamgr, plex, watchask
from .arr import ArrError
from .state import utcnow
from .tooltypes import Candidate, Final, Flow, Step, View

log = logging.getLogger(__name__)


class MediaTools:
    def plex_admin(self) -> plex.Account | None:
        """The Plex account of an admin (collections need the server owner)."""
        if not self.plex_store or self.users is None:
            return None
        return next((a for a in self.plex_store.all() if self.is_admin(a.user_id)), None)

    async def _t_admin_plex_collection(self, chat_id: int, action: str, name: str | None = None,
                                       titles: list[dict] | None = None) -> Final:
        acc = self.plex_admin()
        if not (self.plex and acc):
            raise ArrError("Dafür muss ein Admin sein Plex verbinden (/plex) – Sammlungen legt der Server-Besitzer an.")
        if action in ("auto_on", "auto_off"):
            self.plex_store.set_meta("auto_collections", "1" if action == "auto_on" else None)  # type: ignore[union-attr]
            if action == "auto_off":
                return Final("📚 Keine automatischen Wunsch-Sammlungen mehr (die vorhandenen bleiben in Plex).")
            done = await self.sync_wish_collections()
            return Final("📚 Ab jetzt hat jeder eine Sammlung „Wünsche von …“ in Plex, täglich aktualisiert."
                         + (f"\n{done}" if done else ""))
        if not name or not titles:
            raise ArrError("Für eine Sammlung brauche ich einen Namen und die Titel.")
        try:
            library = await self.plex.library(acc)
            items, missing = collections_plex.match(library, titles)
            if not items:
                return Final(f"📚 Von den {len(titles)} Titeln für „{name}“ ist keiner in Plex.")
            res = await collections_plex.Collections(self.plex).put(acc, name.strip()[:60], items, missing)
        except plex.PlexError as e:
            raise ArrError(str(e)) from e
        return Final(res.text())

    async def sync_wish_collections(self) -> str:
        """'Wünsche von <Name>' for every user with requests that are in Plex (daily when switched on)."""
        acc = self.plex_admin()
        if not (self.plex and acc and self.users is not None):
            return ""
        library = await self.plex.library(acc)
        coll = collections_plex.Collections(self.plex)
        lines = []
        for uid in sorted({r["chat_id"] for r in self.store.state.requests}):
            if not self.users.is_allowed(uid):
                continue
            items = collections_plex.wish_items(self.store.state.requests, uid, library)
            if not items:
                continue
            res = await coll.put(acc, f"Wünsche von {self.users.name(uid)}", items)
            if res.added:
                lines.append(f"„Wünsche von {self.users.name(uid)}“: +{len(res.added)}")
        return "\n".join(lines)

    async def _t_admin_cleanup(self, chat_id: int, action: str, what: str | None = None, kind: str = "all",
                               days: int | None = None, low_space_gb: int | None = None, auto: bool = False,
                               rule_id: int | None = None) -> Final:
        if self.cleanup is None:
            raise ArrError("Aufräumregeln sind nicht eingerichtet.")
        if action == "add":
            if not what or not days:
                raise ArrError("Für eine Regel brauche ich: gesehen oder ungesehen, und nach wie vielen Tagen.")
            rule = self.cleanup.add(what, kind or "all", int(days), low_space_gb, bool(auto))
            return Final(f"🧹 Regel {rule.id} angelegt: {rule.text}.\nIch prüfe täglich um 11 Uhr – "
                         "oder sofort: „Aufräumregeln jetzt prüfen“.")
        if action == "remove":
            if rule_id is None or not self.cleanup.remove(int(rule_id)):
                raise ArrError(f"Regel {rule_id} gibt es nicht.")
            return Final(f"🗑 Regel {rule_id} gelöscht.")
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "cleanup", arg=action == "run"))
        return Final("")

    def _dupe_prefs(self) -> dict:
        return dupes.load_prefs(self.cleanup.setting("dupes") if self.cleanup else None)

    async def _t_admin_duplicates(self, chat_id: int, action: str = "scan", kind: str = "all",
                                  similarity: float | None = None, languages: list[str] | None = None,
                                  resolutions: str | None = None, language_first: bool | None = None) -> Final:
        prefs = self._dupe_prefs()
        changed = False
        if similarity is not None:
            prefs["similarity"] = max(50.0, min(100.0, float(similarity)))
            changed = True
        if languages:
            prefs["languages"] = [x for x in (dupes.norm_language(v) for v in languages) if x]
            changed = True
        if resolutions:
            prefs["resolutions"] = dupes.parse_resolutions(resolutions)
            changed = True
        if language_first is not None:
            prefs["language_first"] = bool(language_first)
            changed = True
        if changed and self.cleanup is not None:
            self.cleanup.set_setting("dupes", json.dumps(prefs))
        if action == "settings":
            return Final("🧬 " + dupes.prefs_text(prefs))
        acc = self._plex_of(chat_id)
        if not (self.plex and acc):
            raise ArrError("Der Duplikat-Finder braucht Plex – verbinde es mit /plex (als Server-Besitzer).")
        metas: list[dict] = []
        try:
            for s in await self.plex.sections(acc):
                if s.get("type") == "movie" and kind in ("all", "movie"):
                    metas += await self.plex.section_items(acc, s["key"])
                elif s.get("type") == "show" and kind in ("all", "series"):
                    metas += await self.plex.section_items(acc, s["key"], episodes=True)
        except plex.PlexError as e:
            raise ArrError(str(e)) from e
        # Audio languages only come with the details: fetch them for the titles that matter.
        wanted = [m for g in dupes.groups(metas) for m in g]
        rest, wanted = wanted[400:], wanted[:400]  # the rest without audio details, but not left out
        sem = asyncio.Semaphore(4)

        async def detail(m: dict) -> dict:
            async with sem:
                try:
                    full = (await self.plex.metadata(acc, str(m.get("ratingKey")))) or {}
                except plex.PlexError:
                    return m
            return {**m, "Media": full.get("Media") or m.get("Media")}

        job = dupes.find(list(await asyncio.gather(*(detail(m) for m in wanted))) + rest, prefs)
        job.no_details = len(rest)
        text, buttons = dupes.full_list(job) if action == "list" and job.items else dupes.preview(job, prefs)
        if not buttons:
            return Final(text)
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Duplikate", None), stage="dup", data=job)
        flow.step = Step(text, buttons)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _delete_dupes(self, chat_id: int, job: Any, items: list[Any]) -> None:
        acc = self._plex_of(chat_id)
        rescan: set[tuple] = set()
        for d in items:
            try:
                await self.plex.delete_media(acc, d.drop.rating_key, d.drop.media_id)
            except plex.PlexError as e:
                job.failed.append(f"{d.name}: {e}")
                continue
            job.deleted.append(d)
            if d.ext:
                rescan.add(d.ext)
        for kind, ext in rescan:  # Radarr/Sonarr: notice which file is left
            try:
                if kind == "movie" and self.radarr:
                    hit = next((m for m in await self.radarr.library() if m.get("tmdbId") == ext), None)
                    if hit:
                        await self.radarr.command("RescanMovie", movieIds=[hit["id"]])
                elif kind == "series" and self.sonarr:
                    hit = next((s for s in await self.sonarr.library()
                                if self._norm(s.get("title") or "") == self._norm(str(ext))), None)
                    if hit:
                        await self.sonarr.command("RescanSeries", seriesId=hit["id"])
            except ArrError as e:
                log.info("Neu einlesen nach Duplikat: %s", e)

    async def _dup_step(self, flow: Flow, value: str) -> Step:
        job = flow.data
        if value == "no":
            return Step(dupes.summary(job, cancelled=True), done=True)
        if value == "all":
            await self._delete_dupes(flow.chat_id, job, [d for d in job.items if d.sure])
            return Step(dupes.summary(job), done=True)
        if value in ("del", "skip") and job.pos < len(job.items):
            if value == "del":
                await self._delete_dupes(flow.chat_id, job, [job.items[job.pos]])
            else:
                job.skipped += 1
            job.pos += 1
        elif value == "one":
            job.pos = 0
        elif value == "list" or value.startswith("pg:"):
            job.page += {"pg:next": 1, "pg:prev": -1}.get(value, 0) if value != "list" else -job.page
            text, buttons = dupes.full_list(job)
            return Step(text, buttons)
        if value == "stop" or job.pos >= len(job.items):
            return Step(dupes.summary(job), done=True)
        text, buttons = dupes.one(job)
        return Step(text, buttons)

    def _plex_of(self, chat_id: int) -> plex.Account | None:
        if not self.plex_store:
            return None
        return self.plex_store.get(chat_id) or self.plex_admin()

    async def _media_report(self) -> Any:
        from . import media

        accounts = [a for a in (self.plex_store.all() if self.plex_store else [])
                    if self.users is None or self.users.is_allowed(a.user_id)]
        return await media.collect(self.radarr, self.sonarr, self.plex if accounts else None, accounts,
                                   german_check=False)

    def _recent_requests(self) -> dict[tuple[str, int], int]:
        cutoff = utcnow() - timedelta(days=mediamgr.PROTECT_DAYS)
        out: dict[tuple[str, int], int] = {}
        for r in self.store.state.requests:
            try:
                at = datetime.fromisoformat(r.get("at") or "")
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            ext = r.get("tmdb") if r["kind"] == "movie" else r.get("tvdb")
            if at >= cutoff and ext:
                out[(r["kind"], ext)] = r["chat_id"]
        return out

    async def _t_admin_delete_media(self, chat_id: int, kind: str = "all", watched: bool | None = None,
                                    older_days: int | None = None, seen_days: int | None = None,
                                    min_gb: float | None = None, max_rating: float | None = None,
                                    genres: list[str] | None = None, ended: bool | None = None,
                                    scope: str | None = None, largest: int | None = None) -> Final:
        q = mediamgr.Query(kind or "all", watched, older_days, seen_days, min_gb, max_rating, list(genres or []),
                           ended, scope or "", largest)
        if q.kind == "movie" and not self.radarr or q.kind == "series" and not self.sonarr:
            raise ArrError("Dafür fehlt Radarr bzw. Sonarr.")
        acc = self._plex_of(chat_id)
        if (watched is not None or seen_days) and not (self.plex and acc):
            raise ArrError("Für „gesehen“ brauche ich deinen Plex-Verlauf – verbinde Plex mit /plex.")
        if not any((watched is not None, older_days, seen_days, min_gb, max_rating, genres, largest, ended)):
            raise ArrError("Das wäre ALLES. Was genau soll weg (gesehen, ungesehen, älter als …, die größten …)? "
                           "Bitte nachfragen.")
        report = await self._media_report()
        kept = self.cleanup.kept() if self.cleanup else set()
        name_of = (lambda u: self.users.name(u)) if self.users is not None else (lambda u: str(u))
        hits, protected = mediamgr.select(report, q, acc.user_id if acc else None, kept, self._recent_requests(),
                                          name_of)
        job = mediamgr.Job(q, protected=protected)
        series = [t for t in hits if t.kind == "series"]
        job.items = [mediamgr.whole(t) for t in hits if t.kind == "movie"]
        if series and not q.scope:
            job.pending_series = series
            text, buttons = mediamgr.scope_question(job)
        else:
            await self._plan_series(job, series, acc)
            text, buttons = mediamgr.preview(job)
        if not buttons:
            return Final(text)
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Aufräumen", None), stage="mm", data=job)
        flow.step = Step(text, buttons)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _plan_series(self, job: Any, series: list[Any], acc: plex.Account | None) -> None:
        q = job.query
        if q.scope in ("", "series"):
            job.items += [mediamgr.whole(t) for t in series]
        else:
            for t in series:
                try:
                    eps = await self.sonarr.episodes(t.arr_id)
                    key = t.plex_keys.get(acc.user_id) if acc else None
                    leaves = await self.plex.episodes(acc, key) if (key and self.plex) else []
                except (ArrError, plex.PlexError) as e:
                    job.failed.append(f"{t.title}: {e}")
                    continue
                job.items += mediamgr.plan_parts(t, eps, leaves, q, acc.user_id if acc else None)
        job.items.sort(key=lambda i: -i.size)
        if q.largest:
            job.items = job.items[: q.largest]

    async def _mm_step(self, flow: Flow, value: str) -> Step:
        job = flow.data
        if value == "no":
            return Step(mediamgr.summary(job, cancelled=True), done=True)
        if value.startswith("sc:"):
            job.query.scope = value[3:]
            series, job.pending_series = job.pending_series, []
            await self._plan_series(job, series, self._plex_of(flow.chat_id))
            text, buttons = mediamgr.preview(job)
            return Step(text, buttons, done=not buttons)
        if value == "all":
            for item in job.items:
                err = await mediamgr.delete(self.radarr, self.sonarr, item)
                (job.failed.append(err) if err else job.deleted.append(item))
            return Step(mediamgr.summary(job), done=True)
        if value in ("del", "skip", "keep") and job.pos < len(job.items):
            item = job.items[job.pos]
            if value == "del":
                err = await mediamgr.delete(self.radarr, self.sonarr, item)
                (job.failed.append(err) if err else job.deleted.append(item))
            elif value == "keep" and self.cleanup is not None:
                title = item.name.split(" – ")[0].split(": ")[0]
                self.cleanup.keep(item.service, item.arr_id, title)
                job.kept.append(title)
                # the other parts of the same series are protected now, too
                job.items = job.items[: job.pos + 1] + [i for i in job.items[job.pos + 1:]
                                                        if (i.service, i.arr_id) != (item.service, item.arr_id)]
            else:
                job.skipped += 1
            job.pos += 1
        elif value == "one":
            job.pos = 0
        if value == "stop" or job.pos >= len(job.items):
            return Step(mediamgr.summary(job), done=True)
        text, buttons = mediamgr.one(job)
        return Step(text, buttons)

    async def _t_admin_storage(self, chat_id: int, top: int | None = None) -> Final:
        from . import extras

        report = await self._media_report()
        disks = await extras.disks(self.radarr, self.sonarr)
        acc = self._plex_of(chat_id)
        self._outbox.setdefault(chat_id, []).append(
            View(chat_id, "html", arg=mediamgr.storage_text(report, disks, acc.user_id if acc else None,
                                                        max(3, min(int(top or 10), 25)))))
        return Final("")

    async def _t_admin_keep(self, chat_id: int, title: str | None = None, kind: str | None = None,
                            off: bool = False) -> Final:
        if self.cleanup is None:
            raise ArrError("Nicht eingerichtet.")
        if not title:
            rows = self.cleanup.kept_titles()
            if not rows:
                return Final("🛡 Noch nichts geschützt. Z. B. „Dune nie löschen“.")
            return Final("🛡 Nie löschen:\n" + "\n".join(f"{'🎬' if s == 'radarr' else '📺'} {t}" for s, _, t in rows))
        kinds = [kind] if kind else [k for k, svc in (("movie", self.radarr), ("series", self.sonarr)) if svc]
        found, errors = [], []
        for k in kinds:
            try:
                found.append((k, await self._find_in_library(k, title)))
            except ArrError as e:
                errors.append(str(e))
        if not found:
            raise ArrError(errors[0] if errors else f"{title} nicht gefunden.")
        if len(found) > 1:
            raise ArrError(f"„{title}“ gibt es als Film und als Serie – welches meinst du?")
        k, lib_id = found[0]
        item = await self._svc(k).get(lib_id)
        service = "radarr" if k == "movie" else "sonarr"
        if off:
            self.cleanup.unkeep(service, lib_id)
            return Final(f"🔓 {item.get('title')} ist nicht mehr geschützt.")
        self.cleanup.keep(service, lib_id, item.get("title") or title)
        return Final(f"🛡 {item.get('title')} wird nie gelöscht – auch nicht von Aufräumregeln.")

    async def _t_admin_watch_questions(self, chat_id: int, on: bool | None = None, every_days: int | None = None,
                                       max: int | None = None, after_days: int | None = None,
                                       now: bool = False) -> Final:
        if self.cleanup is None:
            raise ArrError("Nicht eingerichtet.")
        s = watchask.load(self.cleanup)
        changed = False
        for attr, val, lo, hi in (("every", every_days, 1, 60), ("max", max, 1, 10), ("after", after_days, 0, 90)):
            if val is not None:
                setattr(s, attr, min(hi, builtins_max(lo, int(val))))
                changed = True
        if on is not None:
            s.on, changed = bool(on), True
        if changed:
            watchask.save(self.cleanup, s)
        if not now:
            return Final(s.text())
        n = await self.watch_questions(chat_id)
        return Final("" if n else "👍 Gerade nichts: Kein Titel, den du gesehen hast und nach dem ich noch nicht gefragt habe.")

    async def watch_questions(self, chat_id: int) -> int:
        """Questions about titles the admin saw in Plex ("Dune gesehen – löschen?"), into the outbox."""
        acc = self.plex_store.get(chat_id) if self.plex_store else None
        if not (self.cleanup and self.plex and acc and (self.radarr or self.sonarr)):
            return 0
        s = watchask.load(self.cleanup)
        now = utcnow()
        report = await self._media_report()
        movies, series = watchask.candidates(report, acc.user_id, self.cleanup.kept(), self._recent_requests(),
                                             s.after, now)
        titles = sorted(movies + series, key=lambda t: t.viewed_by.get(acc.user_id) or now, reverse=True)
        asks: list[watchask.Ask] = []
        for t in titles:
            if len(asks) >= s.max:
                break
            if t.kind == "movie":
                a: watchask.Ask | None = watchask.Ask(t, [mediamgr.whole(t)])
            else:
                if not self.sonarr:
                    continue
                try:
                    eps = await self.sonarr.episodes(t.arr_id)
                    key = t.plex_keys.get(acc.user_id)
                    leaves = await self.plex.episodes(acc, key) if key else []
                except (ArrError, plex.PlexError) as e:
                    log.info("Nachfrage %s: %s", t.title, e)
                    continue
                a = watchask.series_ask(t, eps, leaves, acc.user_id, now)
            if a is not None and not watchask._answered(self.cleanup, a.key, now):
                asks.append(a)
        for a in asks:
            flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate(a.title.kind, 0, a.title.title, None), stage="wa",
                        data=a)
            flow.step = Step(*watchask.question(a))
            self.flows[flow.id] = flow
            self._outbox.setdefault(chat_id, []).append(flow)
        return len(asks)

    async def _wa_step(self, flow: Flow, value: str) -> Step:
        a = flow.data
        t = a.title
        self.flows.pop(flow.id, None)
        if value in ("del", "whole"):
            items = [a.whole] if value == "whole" and a.whole is not None else a.items
            errors = [e for i in items if (e := await mediamgr.delete(self.radarr, self.sonarr, i))]
            watchask.answer(self.cleanup, a.key, "deleted")
            if errors:
                return Step("⚠️ Nicht gelöscht: " + "; ".join(errors[:3]), done=True)
            freed = sum(i.size for i in items)
            from .media import size_text

            return Step(f"🗑 Gelöscht ({size_text(freed)} frei). Gesperrt ist nichts – einfach wieder hinzufügen, "
                        "wenn du es doch nochmal sehen willst.", done=True)
        if value == "keep":
            self.cleanup.keep(t.service, t.arr_id, t.title)
            return Step(f"🛡 {t.title} wird nie gelöscht – ich frage auch nicht mehr.", done=True)
        if value == "later":
            watchask.answer(self.cleanup, a.key, "later")
            return Step(f"⏰ Okay, ich frage in {watchask.LATER_DAYS} Tagen nochmal.", done=True)
        watchask.answer(self.cleanup, a.key, "no")
        return Step("➡️ Bleibt. Dazu frage ich nicht mehr" + (" (erst wieder bei neuen gesehenen Staffeln)."
                                                             if t.kind == "series" else "."), done=True)

    async def _t_plex_status(self, chat_id: int, title: str | None = None, link: bool = False) -> Final:
        if not (self.plex and self.plex_store):
            return Final("Plex ist nicht eingerichtet.")
        acc = self.plex_store.get(chat_id)
        if link or not acc:
            self._outbox.setdefault(chat_id, []).append(View(chat_id, "plex_link"))
            return Final("" if link else "🔗 Dafür verbinde zuerst dein Plex-Konto:")
        try:
            if not title:
                deck = await self.plex.on_deck(acc)
                self._outbox.setdefault(chat_id, []).append(
                    View(chat_id, "plex", arg={"html": plex.on_deck_text(deck)}))
                return Final("")
            found = await self.plex.search(acc, title)
            if not found:
                return Final(f"🤷 „{title}“ habe ich in Plex nicht gefunden. Noch nicht fertig geladen? /downloads")
            it = found[0]
            meta = await self.plex.metadata(acc, it.rating_key)
            eps = await self.plex.episodes(acc, it.rating_key) if it.kind == "series" else []
        except plex.PlexError as e:
            return Final(f"⚠️ {e}")
        text, key = plex.progress_text(it, meta, eps)
        url = self.plex.web_link(acc, key) if key else None
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "plex", arg={"text": text, "url": url}))
        return Final("")
