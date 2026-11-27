"""German audio: info, fixes, cleaning episodes, notify when German.

A mixin of ToolRunner (arrbot/tools.py): self is the ToolRunner."""

from __future__ import annotations

import asyncio
from datetime import date
import logging
from typing import Any
import uuid

from . import german, manage, watch
from .arr import ArrError, Radarr, Sonarr
from .tmdb import german_summary, TmdbError
from .tooltypes import Final, Flow, NO_TMDB, Step

log = logging.getLogger(__name__)


class GermanTools:
    async def _tmdb_id(self, kind: str, ext_id: int) -> int | None:
        if kind == "movie":
            return ext_id
        return await self.tmdb.tv_from_tvdb(ext_id) if self.tmdb else None

    async def _german_line(self, kind: str, ext_id: int) -> str | None:
        """Warn when there is no German version yet (TMDB), e.g. 'Deutsch erst ab 12.03.'."""
        if not self.tmdb:
            return None
        try:
            tid = await self._tmdb_id(kind, ext_id)
            return german_summary(await self.tmdb.german_info(kind, tid), date.today()) if tid else None
        except TmdbError as e:
            log.info("TMDB: %s", e)
            return None

    async def _series_job(self, chat_id: int, tvdb_id: int, stage: str, text: str,
                          buttons: list[tuple[str, str]], plan: Any) -> Final:
        if not buttons:
            return Final(text)
        cand = await self._candidate(chat_id, "series", int(tvdb_id))
        flow = Flow(uuid.uuid4().hex[:10], chat_id, cand, stage=stage, data=plan)
        flow.step = Step(text, buttons)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _library_series(self, chat_id: int, tvdb_id: int, delete: bool) -> int:
        cand = await self._candidate(chat_id, "series", int(tvdb_id))
        if not cand.lib_id:
            raise ArrError(f"{cand.title} ist nicht in der Bibliothek. Erst hinzufügen (add_series).")
        await self._managed(chat_id, "series", cand.lib_id, perm="repair")
        return cand.lib_id

    async def _t_german_fix(self, chat_id: int, tvdb_id: int | None = None, season: int | None = None,
                            tmdb_id: int | None = None) -> Final:
        if tmdb_id and not tvdb_id:
            return await self._movie_fix(chat_id, int(tmdb_id))
        if not tvdb_id:
            raise ArrError("tvdb_id (Serie) oder tmdb_id (Film) angeben.")
        lib_id = await self._library_series(chat_id, tvdb_id, delete=False)
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        plan = await manage.plan_german(sonarr, lib_id, int(season) if season else None, date.today())
        text, buttons = manage.render_plan(plan)
        return await self._series_job(chat_id, tvdb_id, "gfix", text, buttons, plan)

    @staticmethod
    async def _rescan(sonarr: Any, series_id: int, wait: float = 60.0) -> None:
        """Let Sonarr read the series folder again and wait (max. wait seconds) until it is done."""
        cmd = await sonarr.command("RescanSeries", seriesId=series_id)
        loop = asyncio.get_running_loop()
        end = loop.time() + wait
        while cmd.get("id") and loop.time() < end:
            state = await sonarr._req("GET", f"/command/{cmd['id']}") or {}
            if state.get("status") in ("completed", "failed", "aborted", "cancelled"):
                return
            await asyncio.sleep(2)

    async def _t_clean_episodes(self, chat_id: int, tvdb_id: int, which: str, season: int | None = None) -> Final:
        if which == "season" and not season:
            raise ArrError("Welche Staffel? season angeben.")
        lib_id = await self._library_series(chat_id, tvdb_id, delete=True)
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        plan = await manage.plan_clean(sonarr, lib_id, which, int(season) if season else None)
        text, buttons = manage.render_clean(plan)
        return await self._series_job(chat_id, tvdb_id, "clean", text, buttons, plan)

    async def _movie_fix(self, chat_id: int, tmdb_id: int) -> Final:
        cand = await self._candidate(chat_id, "movie", tmdb_id)
        if not cand.lib_id:
            raise ArrError(f"{cand.title} ist nicht in Radarr.")
        await self._managed(chat_id, "movie", cand.lib_id, perm="repair")
        radarr: Radarr = self._need(self.radarr, "Radarr")
        fix = await manage.plan_movie(radarr, cand.lib_id)
        text, buttons = manage.render_movie(fix)
        if not buttons:
            return Final(text)
        flow = Flow(uuid.uuid4().hex[:10], chat_id, cand, stage="mfix", data=fix)
        flow.step = Step(text, buttons)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _run_series_job(self, flow: Flow, value: str) -> Step:
        plan, title = flow.data, flow.cand.title
        if value == "ok" and flow.stage in ("gfix", "mfix"):
            kind, arr_id = ("m", plan.movie_id) if flow.stage == "mfix" else ("s", plan.series_id)
            german.confirmed.add(kind, arr_id)
            return Step(f"👍 Gemerkt: {title} ist Deutsch, nur die Tonspur ist falsch markiert. "
                        "Ich melde das nicht mehr.", done=True)
        if value == "notify" and flow.stage == "gfix":
            res = await self._t_notify_german(flow.chat_id, "series", flow.cand.ext_id)
            return Step(res.text or f"🔔 Ich sage Bescheid, sobald es {title} auf Deutsch gibt.", done=True)
        if value == "rescan" and flow.stage == "gfix":
            sonarr_ = self._need(self.sonarr, "Sonarr")
            try:
                await self._rescan(sonarr_, plan.series_id)
                flow.data = plan = await manage.plan_german(sonarr_, plan.series_id, plan.season, date.today())
            except ArrError as e:
                return Step(f"❌ Neu einlesen hat nicht geklappt: {e}", done=True)
            text, buttons = manage.render_plan(plan)
            text = "🔄 Sonarr hat den Ordner neu eingelesen.\n" + text
            return Step(text, [b for b in buttons if b[1] != "rescan"]) if len(buttons) > 1 else Step(text, done=True)
        if value == "more" and flow.stage == "gfix":
            try:
                await manage.search_more(self._need(self.sonarr, "Sonarr"), plan)
            except ArrError as e:
                return Step(f"❌ Suche hat nicht geklappt: {e}", done=True)
            text, buttons = manage.render_plan(plan)
            return Step(text, buttons) if buttons else Step(text, done=True)
        if value not in ("all", "missing", "research", "drop"):
            return Step(f"👍 Okay, bei {title} bleibt alles, wie es ist.", done=True)
        if flow.stage == "mfix":
            radarr: Radarr = self._need(self.radarr, "Radarr")
            try:
                text = await manage.run_movie(radarr, flow.data)
            except ArrError as e:
                return Step(f"❌ Hat nicht geklappt: {e}", done=True)
            self.store.track("radarr", flow.data.movie_id, title, flow.chat_id)
            await self._added(flow.chat_id, f"🇩🇪 {title}: deutsche Fassung wird geladen")
            return Step(text, done=True)
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        try:
            if flow.stage == "gfix":
                text = await manage.run_fix(sonarr, plan, only_missing=value == "missing")
                await self._added(flow.chat_id, f"🇩🇪 {title}: deutsche Folgen geladen")
            else:
                text = await manage.run_clean(sonarr, plan, research=value == "research")
                await self._added(flow.chat_id, f"🗑 {title}: {len(plan.episodes)} Folgen gelöscht")
        except ArrError as e:
            return Step(f"❌ Hat nicht geklappt: {e}", done=True)
        self.store.track("sonarr", plan.series_id, title, flow.chat_id)  # report the new downloads
        return Step(text, done=True)

    async def _german_titles(self, kind: str, tmdb_ids: list[int | None]) -> dict[int, str]:
        ids = [i for i in tmdb_ids if i]
        if not self.tmdb or not ids:
            return {}
        return await self.tmdb.german_titles(kind, ids)

    async def _t_german_info(self, chat_id: int, kind: str, id: int, season: int | None = None,  # noqa: A002
                             indexer: bool | None = None) -> Final:
        """Rendered in code: the AI only picks the title, it never paraphrases dates (that went wrong)."""
        today = date.today()
        if kind == "movie":
            if not self.tmdb:
                return Final(NO_TMDB)
            text = await german.movie_report(self._need(self.radarr, "Radarr"), self.tmdb, int(id),
                                             bool(indexer), today)
        else:
            # Series: checking the indexers is the only real proof of German episodes, so it is on by default.
            text = await german.series_report(self._need(self.sonarr, "Sonarr"), self.tmdb, int(id),
                                              int(season) if season else None, indexer is not False, today)
        return Final(text)

    async def _t_notify_german(self, chat_id: int, kind: str, id: int, season: int | None = None,  # noqa: A002
                               stop: bool = False) -> Final:
        if not self.profiles:
            return Final("Profile sind nicht eingerichtet.")
        if stop:
            gone = [w for w in self.profiles.watches(chat_id) if w["kind"] == kind and w["ext_id"] == int(id)]
            for w in gone:
                self.profiles.drop_watch(w["id"])
            return Final(f"🔕 Okay, für {gone[0]['title']} melde ich mich nicht mehr." if gone
                         else "Dafür hatte ich keine Benachrichtigung.")
        if kind == "movie" and not self.tmdb:
            return Final(NO_TMDB)
        cand = await self._candidate(chat_id, kind, int(id))
        if kind == "series" and not cand.lib_id:
            return Final(f"📺 {cand.title} ist noch nicht in Sonarr. Füg die Serie zuerst hinzu, dann prüfe ich "
                         "täglich, ob es neue deutsche Folgen gibt.")
        title = cand.title + (f" ({cand.year})" if kind == "movie" and cand.year else "")
        what = f"{title} Staffel {season}" if season else title
        self.profiles.add_watch(chat_id, kind, int(id), title, int(season) if season else None)
        w = [x for x in self.profiles.watches(chat_id) if x["kind"] == kind and x["ext_id"] == int(id)
             and x["season"] == (int(season) if season else None)]
        notices = await watch.check_watches(self, only=w)  # maybe it's already there
        if notices:
            n = notices[0]
            return Final(n.text) if n.text else Final("")
        return Final(f"🔔 Mach ich! Ich prüfe {what} jeden Tag und melde mich, sobald es "
                     f"{'eine deutsche Fassung gibt' if kind == 'movie' else 'neue deutsche Folgen gibt'}.")
