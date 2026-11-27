"""Tools exposed to the language model, and the button flows that run without it.

Token budget matters: the AI only finds the title. Picking a candidate, seasons and
quality happens through buttons handled here in code, without further AI calls.

Map (see CLAUDE.md): tooltypes = data classes and helpers, toolspecs = what the AI sees, mixins tools_media /
tools_admin / tools_discover / tools_german. This file: the ToolRunner core – rights, run, button flows,
adding, searching, removing, pausing."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import timedelta, tzinfo
import logging
import re
from typing import Any, Awaitable, Callable
import uuid
from zoneinfo import ZoneInfo

from . import (approval, debuglog, manage, plex, profiles, qprofiles, recipes, sitewatch, urlinfo,
    users as users_mod, watch, wishlist)
from .arr import ArrError, Radarr, Sonarr
from .llm import ToolCall, ToolResult, ToolSpec
from .state import Store, utcnow
from .tmdb import TMDB, TmdbError
from .tools_admin import AdminTools
from .tools_discover import DiscoverTools
from .tools_german import GermanTools
from .tools_media import MediaTools
from .toolspecs import build_admin_specs, build_specs
from .tooltypes import (Candidate, _compact, DownloadsView, _dump, Final, Flow, Held, MAX_RESULTS,
    MAX_SEASON_BUTTONS, _movie_cand, mute, Offer, queue_title, release_languages, resume_search, _season_buttons,
    _season_overview, _seasons_text, _series_cand, Step, View, _what, _year_filter)

log = logging.getLogger(__name__)

__all__ = ["ToolRunner", "ArrError", "Candidate", "DownloadsView", "Final", "Flow", "Held", "Offer", "Step", "View", "build_admin_specs", "build_specs", "mute", "queue_title", "release_languages", "resume_search", "_season_overview", "_seasons_text"]


class ToolRunner(MediaTools, AdminTools, DiscoverTools, GermanTools):
    def __init__(
        self,
        radarr: Radarr | None,
        sonarr: Sonarr | None,
        store: Store,
        min_availability: str,
        quality_prompt_seconds: int = 0,
        tmdb: TMDB | None = None,
        tz: tzinfo | None = None,
    ):
        self.tmdb = tmdb
        self.tz = tz or ZoneInfo("Europe/Berlin")
        # Called after something was added/searched: (chat_id, text). The bot tells the admin.
        self.on_added: Callable[[int, str], Awaitable[None]] | None = None
        self.is_admin: Callable[[int], bool] = lambda _uid: False  # set by the bot
        self.notify_user: Callable[[int, str], Awaitable[None]] | None = None
        self.radarr = radarr
        self.sonarr = sonarr
        self.store = store
        self.min_availability = min_availability
        self.quality_prompt_seconds = quality_prompt_seconds
        self.specs = build_specs(radarr is not None, sonarr is not None, tmdb is not None)
        self.flows: dict[str, Flow] = {}
        self.offers: dict[str, Offer] = {}
        self.rating_asks: dict[str, Any] = {}  # "wie fandest du X?" -> the title
        self._outbox: dict[int, list[Flow | Offer | View]] = {}
        self.users: Any = None  # UserStore, set by the bot (admin tools)
        self.admin_specs = build_admin_specs()
        self._seen: dict[int, dict[tuple[str, int], Candidate]] = {}
        self.url_options: dict = {}  # tests inject a fake transport here
        self.profiles: profiles.ProfileStore | None = None  # set by the bot
        self.plex: plex.Plex | None = None  # set by the bot
        self.plex_store: plex.PlexStore | None = None
        self.cleanup: Any = None  # cleanup.RuleStore, set by the bot
        self.sites: sitewatch.SiteStore | None = None  # set by the bot
        self.wishes: wishlist.WishStore | None = None  # set by the bot
        self.recipes: recipes.RecipeStore | None = None  # set by the bot
        self.bot_info: Callable[[int], Awaitable[str]] | None = None  # version and models, set by the bot
        self.builder: Any = None  # LLM provider that writes recipes (a stronger model)
        self.builder_model = ""
        self.builder_cost: Callable[[dict], float | None] = lambda _u: None
        self.on_llm_usage: Callable[[int, dict, float | None], None] | None = None  # bot books it
        self.site_hours = 6
        self.approvals: approval.ApprovalStore | None = None  # set by the bot
        self.on_approval: Callable[[approval.Pending], Awaitable[None]] | None = None  # bot asks the admins
        self.running: dict[int, tuple[str, float]] = {}  # chat -> (tool, since)
        self.trace: dict[int, list[tuple[str, str | None, str]]] = {}

    # ---- plumbing -------------------------------------------------------
    def take_outbox(self, chat_id: int) -> list[Flow | Offer | View]:
        """Button messages created during the last AI turn; the bot sends them."""
        return self._outbox.pop(chat_id, [])

    # Users only find and add titles; anything that changes or deletes what is there, costs extra or
    # watches in the background is for admins.
    ADMIN_ONLY = {"german_fix", "clean_episodes", "remove_title", "pause_title", "resume_title", "watch_site",
                  "build_tool"}

    def _admin(self, uid: int) -> bool:
        """Without user management (single-user setup) whoever may use the bot is the admin."""
        return self.users is None or self.is_admin(uid)

    # Tools a user may get with an extra right from the admin ("Sarah darf ihre Filme löschen").
    TOOL_PERMS = {"remove_title": ("delete", "delete_own"), "pause_title": ("pause",), "resume_title": ("pause",),
                  "german_fix": ("repair",), "clean_episodes": ("repair",), "watch_site": ("sites",),
                  "admin_delete_media": ("media",), "admin_storage": ("media",), "admin_duplicates": ("media",),
                  "admin_keep": ("media",)}

    def _admin_only(self, name: str) -> bool:
        return name in self.ADMIN_ONLY or name.startswith(("admin_", "recipe_"))

    def may(self, uid: int, name: str) -> bool:
        """May uid run this tool? Admins: everything. Users: search/add, plus what the admin allowed them."""
        if self._admin(uid):
            return True
        if self.users is None or not self.users.is_allowed(uid):
            return False
        perms = self.TOOL_PERMS.get(name)
        if perms:
            return any(self.users.has_perm(uid, p) for p in perms)
        return not self._admin_only(name)

    def specs_for(self, user_id: int) -> list[ToolSpec]:
        if self.users is not None and self.is_admin(user_id):
            return self.specs + self.admin_specs + self.recipe_specs()
        if self.users is None:
            return self.specs
        return [s for s in self.specs + self.admin_specs if self.may(user_id, s.name)]

    def recipe_specs(self) -> list[ToolSpec]:
        return [r.spec() for r in self.recipes.all()] if self.recipes else []

    # ---- quality limit per user ----------------------------------------------------------------
    def max_quality(self, uid: int) -> int:
        return self.users.max_quality(uid) if self.users is not None else 0

    async def allowed_profiles(self, kind: str, uid: int) -> list[dict]:
        profiles = await self._svc(kind).quality_profiles()
        cap = self.max_quality(uid)
        return [p for p in profiles if not cap or 0 < qprofiles.max_resolution(p) <= cap]

    async def _check_cap(self, kind: str, uid: int, profile: str) -> None:
        cap = self.max_quality(uid)
        if cap and profile not in [p["name"] for p in await self.allowed_profiles(kind, uid)]:
            allowed = ", ".join(p["name"] for p in await self.allowed_profiles(kind, uid)) or "keins"
            raise ArrError(f"„{profile}“ ist über deiner Grenze ({users_mod.quality_label(cap)}). Möglich: {allowed}")

    async def _capped_profile(self, flow: Flow) -> str | None:
        """The profile to add with: what was chosen, unless it is above the user's limit."""
        cap = self.max_quality(flow.chat_id)
        if not cap:
            return flow.profile
        svc = self._svc(flow.cand.kind)
        chosen = await svc.resolve_profile(flow.profile)
        if 0 < qprofiles.max_resolution(chosen) <= cap:
            return chosen["name"]
        allowed = await self.allowed_profiles(flow.cand.kind, flow.chat_id)
        if not allowed:
            raise ArrError(f"Es gibt kein Qualitätsprofil bis {users_mod.quality_label(cap)} – sag dem Admin Bescheid.")
        if flow.profile:  # asked for more than allowed
            raise ArrError(f"„{chosen['name']}“ ist über deiner Grenze ({users_mod.quality_label(cap)}). "
                           f"Möglich: {', '.join(p['name'] for p in allowed)}")
        return max(allowed, key=qprofiles.max_resolution)["name"]

    # ---- buttons that survive a restart ----------------------------------------------------------
    KEEP_STAGES = ("", "seasons", "quality", "extend")  # flows with a plan (gfix, clean ...) are rebuilt on demand
    KEEP_MAX = 300

    def dump_buttons(self) -> dict:
        flows = [asdict(f) for f in self.flows.values() if f.stage in self.KEEP_STAGES and f.data is None]
        offers = [asdict(o) for o in self.offers.values()]
        return {"flows": flows[-self.KEEP_MAX:], "offers": offers[-self.KEEP_MAX:]}

    def load_buttons(self, data: dict) -> int:
        n = 0
        for f in data.get("flows") or []:
            try:
                f = dict(f)
                cand = Candidate(**f.pop("cand"))
                step = f.pop("step", None)
                flow = Flow(cand=cand, step=Step(**{**step, "buttons": [tuple(b) for b in step["buttons"]]})
                            if step else None, **f)
                self.flows.setdefault(flow.id, flow)
                n += 1
            except (TypeError, KeyError) as e:
                log.info("Button-Zustand übersprungen: %s", e)
        for o in data.get("offers") or []:
            try:
                o = dict(o)
                offer = Offer(cands=[Candidate(**c) for c in o.pop("cands")], **o)
                self.offers.setdefault(offer.id, offer)
                n += 1
            except (TypeError, KeyError) as e:
                log.info("Button-Zustand übersprungen: %s", e)
        return n

    def take_trace(self, chat_id: int) -> list[tuple[str, str | None, str]]:
        """(tool, error or None, final text) of every tool run since the last call (activity log)."""
        return self.trace.pop(chat_id, [])

    async def run(self, call: ToolCall, chat_id: int) -> ToolResult:
        self.running[chat_id] = (call.name, asyncio.get_running_loop().time())  # for "arbeitest du noch?"
        try:
            r = await self._run(call, chat_id)
        finally:
            self.running.pop(chat_id, None)
        error = r.content if r.is_error else (r.final if (r.final or "").startswith("⚠️") else None)
        self.trace.setdefault(chat_id, []).append((call.name, error, r.final or ""))
        return r

    async def _run(self, call: ToolCall, chat_id: int) -> ToolResult:
        fn = getattr(self, f"_t_{call.name}", None)
        if fn is None and call.name.startswith("recipe_") and self.recipes and self.recipes.get(call.name):
            name = call.name

            async def fn(chat_id: int, **args: Any) -> Final:
                return await self.run_recipe(chat_id, name, args)
        if fn is None:
            return ToolResult(call.id, call.name, f"Unbekanntes Werkzeug {call.name}", is_error=True)
        if not self.may(chat_id, call.name):
            if call.name.startswith("admin_") and call.name not in self.TOOL_PERMS:
                return ToolResult(call.id, call.name, "Nur für Admins.", is_error=True)
            return ToolResult(call.id, call.name, "Das darf nur der Admin. Du kannst Filme und Serien suchen und "
                              "hinzufügen – für alles andere frag den Admin.", is_error=True)
        try:
            result = await fn(chat_id=chat_id, **call.args)
        except TmdbError as e:
            # Shown as is: the AI would only paraphrase it into "keine Daten gefunden".
            log.warning("TMDB bei %s: %s", call.name, e)
            return ToolResult(call.id, call.name, "ok", final=f"⚠️ {e}")
        except ArrError as e:
            return ToolResult(call.id, call.name, str(e), is_error=True)
        except TypeError as e:
            return ToolResult(call.id, call.name, f"Ungültige Parameter: {e}", is_error=True)
        except Exception as e:  # noqa: BLE001 - report anything to the model instead of crashing
            log.exception("Tool %s fehlgeschlagen", call.name)
            return ToolResult(call.id, call.name, f"Interner Fehler: {e}", is_error=True)
        if isinstance(result, Final):
            return ToolResult(call.id, call.name, "ok", final=result.text)
        return ToolResult(call.id, call.name, _dump(result))

    def _need(self, svc: Any, name: str) -> Any:
        if svc is None:
            raise ArrError(f"{name} ist nicht konfiguriert.")
        return svc

    def _svc(self, kind: str) -> Any:
        return self._need(self.radarr, "Radarr") if kind == "movie" else self._need(self.sonarr, "Sonarr")

    def _remember(self, chat_id: int, c: Candidate) -> None:
        seen = self._seen.setdefault(chat_id, {})
        seen[(c.kind, c.ext_id)] = c
        while len(seen) > 100:
            seen.pop(next(iter(seen)))

    async def _candidate(self, chat_id: int, kind: str, ext_id: int) -> Candidate:
        c = self._seen.get(chat_id, {}).get((kind, ext_id))
        if c:
            return c
        if kind == "movie":
            c = _movie_cand(await self._need(self.radarr, "Radarr").lookup_tmdb(ext_id))
        else:
            c = _series_cand(await self._need(self.sonarr, "Sonarr").lookup_tvdb(ext_id))
        self._remember(chat_id, c)
        return c

    # ---- flows ------------------------------------------------------------
    async def _start_flow(self, chat_id: int, cand: Candidate, **choices: Any) -> Flow:
        flow = Flow(uuid.uuid4().hex[:10], chat_id, cand, **choices)
        self.flows[flow.id] = flow
        flow.step = await self._advance(flow)
        return flow

    async def _advance(self, flow: Flow) -> Step:
        c = flow.cand
        if flow.stage == "extend":
            return await self._extend_step(flow)
        if c.kind == "series" and flow.monitor is None and not flow.seasons:
            if not c.seasons and self.sonarr and c.ext_id:
                # Suggestions and top lists come from TMDB without seasons: ask Sonarr, else it'd be "all".
                try:
                    c.seasons = _series_cand(await self.sonarr.lookup_tvdb(int(c.ext_id))).seasons
                except ArrError as e:
                    log.info("Staffeln von %s nicht gefunden: %s", c.label, e)
            if len(c.seasons) > 1:
                flow.stage = "seasons"
                return Step(f"📺 Welche Staffeln von {c.label}?", _season_buttons(c.seasons))
            flow.monitor = "all"

        if flow.profile is None and self.quality_prompt_seconds > 0:
            profiles = [p["name"] for p in await self.allowed_profiles(c.kind, flow.chat_id)]
            if len(profiles) > 1:
                flow.stage = "quality"
                flow.profiles = profiles
                flow.default_profile = (await self._capped_profile(flow)
                                        or (await self._svc(c.kind).resolve_profile(None))["name"])
                if flow.default_profile not in profiles:
                    flow.default_profile = profiles[0]
                return Step(
                    f"🎚 Welche Qualität für {_what(flow)}?\n"
                    f"Ohne Auswahl nehme ich in {self.quality_prompt_seconds} s „{flow.default_profile}“.",
                    [(("⭐ " if p == flow.default_profile else "") + p, f"p{i}") for i, p in enumerate(profiles)],
                    timeout=self.quality_prompt_seconds,
                )

        flow.stage = "done"
        self.flows.pop(flow.id, None)
        try:
            await self._add(flow)
        except Held as e:
            return Step(f"⏳ {_what(flow)}: {e}", done=True)
        except ArrError as e:
            return Step(f"❌ {c.label} konnte nicht hinzugefügt werden: {e}", done=True)
        except Exception as e:  # noqa: BLE001
            log.exception("Hinzufügen fehlgeschlagen")
            return Step(f"❌ {c.label} konnte nicht hinzugefügt werden: {e}", done=True)
        prof = f" in „{flow.profile}“" if flow.profile else ""
        text = f"✅ {_what(flow)} hinzugefügt{prof}. Ich melde mich, wenn der Download startet."
        german = await self._german_line(c.kind, c.ext_id)
        return Step(text + (f"\n{german}" if german else ""), done=True)

    # ---- extend a series that is already in Sonarr -----------------------------
    async def _start_extend(self, chat_id: int, cand: Candidate) -> Flow | Step:
        series = await self._need(self.sonarr, "Sonarr").get(cand.lib_id)
        have, missing = _season_overview(series)
        name = cand.title
        if not missing:
            more = " Neue Folgen kommen automatisch dazu." if series.get("status") != "ended" else ""
            return Step(f"👍 {name} ist komplett vorhanden ({_seasons_text(have) or 'alle Staffeln'}).{more}", done=True)
        flow = Flow(uuid.uuid4().hex[:10], chat_id, cand, stage="extend", missing=missing)
        self.flows[flow.id] = flow
        have_txt = f"Du hast {_seasons_text(have)}. " if have else ""
        buttons = [(f"⬇️ Alle fehlenden laden ({_seasons_text(missing, short=True)})", "rest")]
        if len(missing) > 1:
            buttons += [(f"Nur Staffel {n}", f"s{n}") for n in missing[:MAX_SEASON_BUTTONS]]
        buttons.append(("Nein, danke", "no"))
        flow.step = Step(
            f"📺 {name} ist schon da. {have_txt}Es fehlen {_seasons_text(missing)}.\nSoll ich sie laden?", buttons
        )
        return flow

    async def _extend_step(self, flow: Flow) -> Step:
        self.flows.pop(flow.id, None)
        if flow.seasons is None:
            return Step(f"👍 Alles klar, {flow.cand.title} bleibt wie es ist.", done=True)
        final = await self._search_existing(flow.chat_id, "series", flow.cand.lib_id, flow.seasons)
        return Step(final.text, done=True)

    async def _hold_rule(self, flow: Flow) -> approval.Rule | None:
        """The approval rule that applies to this wish (genres checked here, size later)."""
        if self.approvals is None or self.is_admin(flow.chat_id):
            return None
        c = flow.cand
        rules = self.approvals.applies(flow.chat_id, c.kind)
        if not rules:
            return None
        genres = list(c.genres)
        if not genres and any(r.genres for r in rules):
            try:
                found = await (self._need(self.radarr, "Radarr").lookup_tmdb(c.ext_id) if c.kind == "movie"
                               else self._need(self.sonarr, "Sonarr").lookup_tvdb(c.ext_id))
                genres = found.get("genres") or []
            except ArrError:
                genres = []
        hits = [r for r in rules if r.matches(flow.chat_id, c.kind, genres)]
        # A rule without size holds everything; otherwise the smallest size limit decides.
        return next((r for r in hits if not r.min_gb), None) or min(hits, key=lambda r: r.min_gb, default=None)

    async def _hold(self, flow: Flow, reason: str, **extra: Any) -> None:
        c = flow.cand
        p = self.approvals.hold(approval.Pending(  # type: ignore[union-attr]
            "", flow.chat_id, c.kind, c.ext_id, _what(flow), reason, flow.monitor, flow.seasons, flow.profile, **extra))
        if self.on_approval:
            await self.on_approval(p)
        raise Held("wartet auf Freigabe durch den Admin. Ich sage dir Bescheid.")

    def _wants_limit(self, uid: int, what: str) -> bool:
        if self.users is None:
            return False
        return any(k.startswith(what + "_") for k in self.users.limits_for(uid))

    async def _check_title_limit(self, uid: int, **counts: int) -> None:
        if self.users is None:
            return
        reason = self.users.title_limit(uid, **counts)
        if reason:
            raise ArrError(f"⛔ {reason}")

    async def _series_counts(self, flow: Flow) -> dict:
        c = flow.cand
        seasons = flow.seasons or [s for s in c.seasons if s > 0]
        counts = {"serien": 1, "staffeln": len(seasons) or 1}
        if self._wants_limit(flow.chat_id, "folgen") and self.tmdb:
            try:
                tmdb_id = c.tmdb_id or (await self._need(self.sonarr, "Sonarr").lookup_tvdb(c.ext_id)).get("tmdbId")
                per = await self.tmdb.season_episodes(int(tmdb_id)) if tmdb_id else {}
            except (ArrError, TmdbError, ValueError):
                per = {}
            counts["folgen"] = sum(n for s, n in per.items() if s in seasons) if seasons else sum(
                n for s, n in per.items() if s > 0)
        return counts

    async def _add(self, flow: Flow, approved: bool = False) -> None:
        c = flow.cand
        counts = {"filme": 1} if c.kind == "movie" else await self._series_counts(flow)
        if not approved:  # the admin said yes: no limit
            await self._check_title_limit(flow.chat_id, **counts)
        flow.profile = await self._capped_profile(flow)
        rule = None if approved else await self._hold_rule(flow)
        if rule and not rule.min_gb:
            await self._hold(flow, f"Genre {' / '.join(rule.genres)}" if rule.genres else "Freigabe nötig")
        if c.kind == "movie":
            radarr = self._need(self.radarr, "Radarr")
            movie = await radarr.add(c.ext_id, flow.profile, self.min_availability, search=rule is None)
            title = f"{c.title or movie['title']} ({movie.get('year')})"
            if rule:  # size rule: look at the release Radarr would take
                try:
                    rel = sorted((r for r in await radarr.releases(movieId=movie["id"]) if manage._usable_any(r)),
                                 key=manage._score, reverse=True)
                except ArrError:
                    rel = []
                size = (rel[0].get("size") or 0) / 2**30 if rel else 0
                if size > (rule.min_gb or 0):
                    await radarr.set_monitored(movie["id"], False)  # Radarr must not grab it by itself
                    await self._hold(flow, f"Download {size:.1f} GB (Grenze {rule.min_gb:g} GB)".replace(".", ","),
                                     arr_id=movie["id"], release=rel[0])
                await radarr.search(movie["id"])
            self.store.track("radarr", movie["id"], title, flow.chat_id)
            self.store.record_request(flow.chat_id, "movie", title, c.ext_id)
            self._learn(flow.chat_id, "requested", "movie", movie, title)
        else:
            series = await self._need(self.sonarr, "Sonarr").add(
                c.ext_id, flow.profile, monitor=flow.monitor or "all", seasons=flow.seasons
            )
            title = c.title or series["title"]
            self.store.track("sonarr", series["id"], title, flow.chat_id)
            self.store.record_request(flow.chat_id, "series", title, series.get("tmdbId"), c.ext_id)
            self._learn(flow.chat_id, "requested", "series", series, title)
        if self.users is not None:
            self.users.record_adds(flow.chat_id, **counts)
        await self._added(flow.chat_id, f"➕ {_what(flow)}")

    async def decide(self, pid: str, ok: bool) -> tuple[approval.Pending | None, str]:
        """Admin pressed Freigeben/Ablehnen. Returns (wish, text for the admin); the user is told here."""
        p = self.approvals.take(pid) if self.approvals else None
        if p is None:
            return None, "Schon erledigt."
        radarr = self.radarr
        try:
            if not ok:
                if p.arr_id and radarr:
                    await radarr.delete(p.arr_id, delete_files=False)
                note = f"🙅 {p.title}: Der Admin hat den Wunsch abgelehnt."
            elif p.arr_id and radarr:  # size check: movie is there, grab the held release
                await radarr.set_monitored(p.arr_id, True)
                try:
                    await radarr.grab(p.release or {})
                except (ArrError, KeyError):
                    await radarr.search(p.arr_id)
                self.store.track("radarr", p.arr_id, p.title, p.user_id)
                self.store.record_request(p.user_id, "movie", p.title, p.ext_id)
                note = f"✅ {p.title}: freigegeben, der Download startet."
            else:
                c = Candidate(p.kind, p.ext_id, p.title, None)
                await self._add(Flow(uuid.uuid4().hex[:10], p.user_id, c, p.monitor, p.seasons, p.profile), approved=True)
                note = f"✅ {p.title}: freigegeben und hinzugefügt."
        except ArrError as e:
            return p, f"❌ {p.title}: {e}"
        if self.notify_user:
            await self.notify_user(p.user_id, note)
        return p, ("✅ Freigegeben: " if ok else "🙅 Abgelehnt: ") + p.title

    async def _added(self, chat_id: int, text: str) -> None:
        if self.on_added:
            try:
                await self.on_added(chat_id, text)
            except Exception:  # noqa: BLE001
                log.exception("on_added fehlgeschlagen")

    async def choose(self, flow_id: str, value: str) -> Flow | None:
        """A button of a flow was pressed. Returns the flow with its next step, or None if stale."""
        flow = self.flows.get(flow_id)
        if flow is None:
            return None
        token = debuglog.LOG.begin(flow.chat_id, f"Button „{value}“ bei {flow.cand.title} ({flow.stage})", "🔘")
        try:
            out = await self._choose(flow, value)
        except Exception as e:
            debuglog.LOG.add("💥", f"{type(e).__name__}: {e}")
            raise
        finally:
            debuglog.LOG.end(token)
        if out is not None and out.step:
            debuglog.LOG.add("📤", out.step.text, flow.chat_id)
        return out

    async def _choose(self, flow: Flow, value: str) -> Flow | None:
        if flow.stage == "remove":
            self.flows.pop(flow.id, None)
            flow.step = await self._do_remove(flow, value)
            return flow
        if flow.stage == "recipe":
            self.flows.pop(flow.id, None)
            flow.step = await self._recipe_step(flow, value)
            if not flow.step.done:
                self.flows[flow.id] = flow
            return flow
        if flow.stage == "wa":
            flow.step = await self._wa_step(flow, value)
            return flow
        if flow.stage == "adv":
            from . import limitadvice

            self.flows.pop(flow.id, None)
            flow.step = Step("👍 Limits bleiben wie sie sind." if value == "no"
                             else limitadvice.apply(flow.data, self.users, value), done=True)
            return flow
        if flow.stage == "dup":
            self.flows.pop(flow.id, None)
            flow.step = await self._dup_step(flow, value)
            if not flow.step.done:
                self.flows[flow.id] = flow
            return flow
        if flow.stage == "mm":
            self.flows.pop(flow.id, None)
            flow.step = await self._mm_step(flow, value)
            if not flow.step.done:
                self.flows[flow.id] = flow
            return flow
        if flow.stage == "qprof":
            self.flows.pop(flow.id, None)
            flow.step = await self._create_profiles(flow, value)
            return flow
        if flow.stage in ("gfix", "clean", "mfix"):
            self.flows.pop(flow.id, None)
            flow.step = await self._run_series_job(flow, value)
            if not flow.step.done:  # "Weiter suchen", "Neu einlesen": the buttons stay
                self.flows[flow.id] = flow
            return flow
        if flow.stage == "extend":
            if value == "rest":
                flow.seasons = flow.missing
            elif value.startswith("s") and value[1:].isdigit():
                flow.seasons = [int(value[1:])]
            elif value != "no":
                return flow
        elif flow.stage == "seasons":
            if value in ("all", "future"):
                flow.monitor = value
            elif value.startswith("s") and value[1:].isdigit():
                flow.seasons = [int(value[1:])]
            else:
                return flow
        elif flow.stage == "quality":
            if not (value.startswith("p") and value[1:].isdigit() and int(value[1:]) < len(flow.profiles)):
                return flow
            flow.profile = flow.profiles[int(value[1:])]
        flow.step = await self._advance(flow)
        return flow

    async def quality_timeout(self, flow_id: str) -> Flow | None:
        flow = self.flows.get(flow_id)
        if flow is None or flow.stage != "quality":
            return None
        return await self.choose(flow_id, f"p{flow.profiles.index(flow.default_profile)}")

    async def add_all(self, offer_id: str) -> str | None:
        """'Alle hinzufügen' under a top list: add every offered title with the default quality."""
        offer = self.offers.pop(offer_id, None)
        if offer is None:
            return None
        lines = []
        for c in offer.cands:
            name = c.title + (f" ({c.year})" if c.year else "")
            try:
                await self._add(Flow(uuid.uuid4().hex[:10], offer.chat_id, c, monitor="all"))
                lines.append(f"✅ {name}")
            except Held:
                lines.append(f"⏳ {name} – wartet auf Freigabe")
            except ArrError as e:
                lines.append(f"❌ {name} – {e}")
        lines.append("\nQualität: Standardprofil. Ich melde mich, wenn Downloads starten.")
        return "\n".join(lines)

    async def choose_offer(self, offer_id: str, idx: int) -> tuple[Offer, Flow | Step] | None:
        """A candidate button was pressed: start its flow (or re-search a library title)."""
        offer = self.offers.pop(offer_id, None)
        if offer is None or not 0 <= idx < len(offer.cands):
            return None
        c = offer.cands[idx]
        if c.lib_id and c.kind == "series":
            res = await self._start_extend(offer.chat_id, c)
            if isinstance(res, Flow):
                res.message_id = offer.message_id
            return offer, res
        if c.lib_id:
            if c.has_file:
                return offer, Step(f"👍 {c.label} ist schon vorhanden.", done=True)
            final = await self._search_existing(offer.chat_id, c.kind, c.lib_id, None)
            return offer, Step(final.text, done=True)
        flow = await self._start_flow(offer.chat_id, c)
        flow.message_id = offer.message_id
        return offer, flow

    # ---- tools -------------------------------------------------------------
    async def _t_search_movies(self, chat_id: int, query: str, year: int | None = None) -> list[dict]:
        res = await self._need(self.radarr, "Radarr").lookup(query)
        res = _year_filter(res, year)[:MAX_RESULTS]
        de = await self._german_titles("movie", [m.get("tmdbId") for m in res])
        out = []
        for m in res:
            c = _movie_cand(m)
            c.title = de.get(c.ext_id) or c.title
            self._remember(chat_id, c)
            out.append(_compact({
                "tmdb": c.ext_id, "title": c.title, "year": c.year,
                "orig": m.get("originalTitle") if m.get("originalTitle") not in (c.title, None) else None,
                "lib_id": c.lib_id, "file": c.has_file,
            }))
        return out

    async def _t_search_series(self, chat_id: int, query: str, year: int | None = None) -> list[dict]:
        res = await self._need(self.sonarr, "Sonarr").lookup(query)
        res = _year_filter(res, year)[:MAX_RESULTS]
        de = await self._german_titles("series", [s.get("tmdbId") for s in res])
        out = []
        for s in res:
            c = _series_cand(s)
            c.title = de.get(s.get("tmdbId")) or c.title
            self._remember(chat_id, c)
            stats = s.get("statistics") or {}
            out.append(_compact({
                "tvdb": c.ext_id, "title": c.title, "year": c.year,
                "last_season": max(c.seasons) if c.seasons else None,
                "ended": s.get("status") == "ended",
                "lib_id": c.lib_id,
                "eps": f"{stats.get('episodeFileCount', 0)}/{stats.get('episodeCount', 0)}" if c.lib_id else None,
            }))
        return out

    async def _t_add_movie(self, chat_id: int, tmdb_id: int, quality_profile: str | None = None) -> Final:
        radarr: Radarr = self._need(self.radarr, "Radarr")
        if quality_profile:
            quality_profile = (await radarr.resolve_profile(quality_profile))["name"]
            await self._check_cap("movie", chat_id, quality_profile)
        cand = await self._candidate(chat_id, "movie", int(tmdb_id))
        if cand.lib_id:
            raise ArrError(f"'{cand.label}' ist schon in Radarr (lib_id {cand.lib_id}, Datei: {cand.has_file}).")
        return self._flow_final(await self._start_flow(chat_id, cand, profile=quality_profile))

    async def _t_add_series(
        self,
        chat_id: int,
        tvdb_id: int,
        monitor: str | None = None,
        seasons: list[int] | None = None,
        quality_profile: str | None = None,
    ) -> Final:
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        if quality_profile:
            quality_profile = (await sonarr.resolve_profile(quality_profile))["name"]
            await self._check_cap("series", chat_id, quality_profile)
        cand = await self._candidate(chat_id, "series", int(tvdb_id))
        if cand.lib_id:
            if seasons:
                return await self._search_existing(chat_id, "series", cand.lib_id, [int(x) for x in seasons])
            res = await self._start_extend(chat_id, cand)
            return Final(res.text) if isinstance(res, Step) else self._flow_final(res)
        flow = await self._start_flow(
            chat_id, cand, monitor=monitor, seasons=[int(s) for s in seasons] if seasons else None,
            profile=quality_profile,
        )
        return self._flow_final(flow)

    def _flow_final(self, flow: Flow) -> Final:
        assert flow.step is not None
        if flow.step.done:
            return Final(flow.step.text)
        self._outbox.setdefault(flow.chat_id, []).append(flow)
        return Final("")

    async def _t_offer_titles(self, chat_id: int, kind: str, ids: list[int]) -> Final:
        cands = [await self._candidate(chat_id, kind, int(i)) for i in ids[:6]]
        if not cands:
            raise ArrError("Keine IDs angegeben.")
        if len(cands) == 1:
            if cands[0].lib_id and kind == "series":
                res = await self._start_extend(chat_id, cands[0])
                return Final(res.text) if isinstance(res, Step) else self._flow_final(res)
            if cands[0].lib_id:
                raise ArrError(f"'{cands[0].label}' ist schon in der Bibliothek (lib_id {cands[0].lib_id}).")
            return self._flow_final(await self._start_flow(chat_id, cands[0]))
        self._offer(chat_id, cands)
        return Final("")

    def _offer(self, chat_id: int, cands: list[Candidate], text: str | None = None, bulk: bool = False,
               suggest: bool = False) -> Offer:
        offer = Offer(uuid.uuid4().hex[:10], chat_id, cands, bulk=bulk, suggest=suggest)
        if text:
            offer.text = text
        self.offers[offer.id] = offer
        while len(self.offers) > 200:
            self.offers.pop(next(iter(self.offers)))
        self._outbox.setdefault(chat_id, []).append(offer)
        return offer

    async def _t_search_existing(
        self, chat_id: int, kind: str, lib_id: int | None = None, seasons: list[int] | None = None,
        title: str | None = None,
    ) -> Final:
        """"Such nochmal": a title that is already in Radarr/Sonarr searches again."""
        if lib_id is None:
            if not title:
                raise ArrError("Welcher Titel? Nenn mir den Namen.")
            lib_id = await self._find_in_library(kind, title)
        return await self._search_again(chat_id, kind, int(lib_id), [int(s) for s in seasons] if seasons else None)

    @staticmethod
    def _norm(s: str) -> str:
        s = re.sub(r"[^a-z0-9äöüß]+", " ", (s or "").lower()).strip()
        return re.sub(r"^(the|der|die|das) ", "", s)

    async def _find_in_library(self, kind: str, title: str) -> int:
        """lib_id of a title in Radarr/Sonarr: exact (also alternative/original titles), else contained.
        Several hits: an error with the candidates so the AI can ask or pick."""
        svc = self._svc(kind)
        want = self._norm(title)
        items = await svc.library()

        def names(i: dict) -> list[str]:
            alt = [a.get("title", "") for a in i.get("alternateTitles") or []]
            return [self._norm(x) for x in [i.get("title", ""), i.get("originalTitle", ""), *alt] if x]

        hits = [i for i in items if want in names(i)]
        if not hits:
            hits = [i for i in items if any(want in n or (len(n) > 3 and n in want) for n in names(i))]
        if not hits:
            # German or other titles Sonarr/Radarr do not keep: ask their title search, it knows more names.
            try:
                found = await svc.lookup(title)
            except ArrError:
                found = []
            in_lib = {i["id"] for i in items}
            hits = [f for f in found[:5] if f.get("id") in in_lib]
        if not hits:
            what = "Film" if kind == "movie" else "Serie"
            raise ArrError(f"{title} ist als {what} nicht in der Bibliothek. Erst hinzufügen "
                           f"({'add_movie' if kind == 'movie' else 'add_series'}).")
        if len({h["id"] for h in hits}) > 1:
            listed = "; ".join(f"{h.get('title')} ({h.get('year')}) lib_id={h['id']}" for h in hits[:6])
            raise ArrError(f"Mehrere Treffer in der Bibliothek: {listed}. Mit lib_id nochmal aufrufen oder nachfragen.")
        return hits[0]["id"]

    async def _search_again(self, chat_id: int, kind: str, lib_id: int, seasons: list[int] | None) -> Final:
        counts: dict = {}
        if self.users is not None and not self._admin(chat_id):
            if kind == "movie":
                counts = {"filme": 1}
            else:
                missing, _ = await self._need(self.sonarr, "Sonarr").search_missing(lib_id, seasons, dry=True)
                counts = {"folgen": len(missing), "staffeln": len({e.get("seasonNumber") for e in missing})}
            await self._check_title_limit(chat_id, **counts)
            self.users.record_adds(chat_id, **counts)
        if kind == "movie":
            radarr: Radarr = self._need(self.radarr, "Radarr")
            movie = await radarr.get(lib_id)
            was_off = not movie.get("monitored", True)
            await radarr.monitor(movie)
            name = f"{movie['title']} ({movie.get('year')})" if movie.get("year") else movie["title"]
            if movie.get("hasFile"):
                note = " Er liegt schon vor – Radarr holt nur eine bessere Fassung, falls das Profil eine will."
            else:
                note = ""
            await radarr.search(movie["id"])
            self.store.track("radarr", movie["id"], name, chat_id)
            await self._added(chat_id, f"🔍 {name} (neue Suche)")
            off = " Er war nicht überwacht – jetzt schon, sonst lädt Radarr nichts." if was_off else ""
            return Final(f"🔍 Neue Suche für {name} gestartet.{off}{note} Ich melde mich, wenn der Download startet.")
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        series = await sonarr.get(lib_id)
        missing, was_off = await sonarr.search_missing(series["id"], seasons)
        where = f" in Staffel {', '.join(map(str, seasons))}" if seasons else ""
        if not missing:
            if seasons:
                self.store.track("sonarr", series["id"], series["title"], chat_id)
                return Final(f"👁 {series['title']}{where.replace(' in ', ': ')} wird überwacht. Bisher fehlt keine "
                             "erschienene Folge; neue lädt Sonarr, sobald sie da sind.")
            return Final(f"👍 Bei {series['title']} fehlt keine bereits erschienene Folge – nichts zu suchen.")
        self.store.track("sonarr", series["id"], series["title"], chat_id)
        await self._added(chat_id, f"🔍 {series['title']}{where} ({len(missing)} Folgen neu gesucht)")
        eps = manage._eps_text(missing)
        off = " Die Serie war nicht überwacht – jetzt schon, sonst lädt Sonarr nichts." if was_off else ""
        n = len(missing)
        return Final(f"🔍 Neue Suche für {series['title']}: {n} fehlende Folge{'n' if n != 1 else ''}"
                     f"{f' ({eps})' if eps else ''}.{off} Ich melde mich, wenn Downloads starten.")

    async def _search_existing(self, chat_id: int, kind: str, lib_id: int, seasons: list[int] | None) -> Final:
        if kind == "movie":
            radarr: Radarr = self._need(self.radarr, "Radarr")
            movie = await radarr.get(lib_id)
            await radarr.search(movie["id"])
            self.store.track("radarr", movie["id"], f"{movie['title']} ({movie.get('year')})", chat_id)
            self.store.record_request(chat_id, "movie", f"{movie['title']} ({movie.get('year')})", movie.get("tmdbId"))
            self._learn(chat_id, "requested", "movie", movie)
            await self._added(chat_id, f"🔍 {movie['title']} (neue Suche)")
            return Final(f"🔍 Suche für {movie['title']} gestartet. Ich melde mich, wenn der Download startet.")
        sonarr: Sonarr = self._need(self.sonarr, "Sonarr")
        series = await sonarr.get(lib_id)
        self.store.record_request(chat_id, "series", series["title"], series.get("tmdbId"), series.get("tvdbId"))
        self._learn(chat_id, "requested", "series", series)
        # Same as "such nochmal": monitors series/seasons/episodes (an unmonitored series is skipped by
        # Sonarr without a word) and searches only what is missing, whole seasons as season packs.
        return await self._search_again(chat_id, "series", series["id"], seasons)

    # ---- delete / pause / resume ---------------------------------------------------------
    def owns(self, uid: int, kind: str, item: dict) -> bool:
        """Did uid request this title (via the bot)?"""
        for r in self.store.state.requests:
            if r["chat_id"] != uid or r["kind"] != kind:
                continue
            if kind == "movie" and r.get("tmdb") and r["tmdb"] == item.get("tmdbId"):
                return True
            if kind == "series" and ((r.get("tvdb") and r["tvdb"] == item.get("tvdbId"))
                                     or (r.get("tmdb") and r["tmdb"] == item.get("tmdbId"))):
                return True
        return False

    async def _managed(self, chat_id: int, kind: str, lib_id: int, delete: bool = False,
                       perm: str | None = None) -> tuple[dict, str, str]:
        """(item, key, title) if chat_id may change this title. perm: delete | pause | repair.
        Admin: always. Users: only with that right from the admin; delete_own / pause only their own titles."""
        svc = self._svc(kind)
        item = await svc.get(int(lib_id))
        service = "radarr" if kind == "movie" else "sonarr"
        key = f"{service}:{item['id']}"
        title = f"{item.get('title')} ({item.get('year')})" if kind == "movie" and item.get("year") else item.get("title", "?")
        if self._admin(chat_id):
            return item, key, title
        perm = perm or ("delete" if delete else "repair")
        has = (lambda p: self.users is not None and self.users.has_perm(chat_id, p))
        if perm == "delete" and has("delete"):
            return item, key, title
        if perm == "delete" and has("delete_own"):
            if self.owns(chat_id, kind, item):
                return item, key, title
            raise ArrError(f"{title} hast nicht du angefragt – löschen darfst du nur deine eigenen Titel.")
        if perm == "pause" and has("pause"):
            if self.owns(chat_id, kind, item):
                return item, key, title
            raise ArrError(f"{title} hast nicht du angefragt – pausieren darfst du nur deine eigenen Titel.")
        if perm == "repair" and has("repair"):
            return item, key, title
        raise ArrError(f"{title} ändern oder löschen kann nur der Admin.")

    async def start_remove(self, chat_id: int, kind: str, lib_id: int) -> Flow:
        item, key, title = await self._managed(chat_id, kind, lib_id, delete=True)
        tool = "Radarr" if kind == "movie" else "Sonarr"
        if kind == "movie":
            has_files = bool(item.get("hasFile"))
            size = (item.get("sizeOnDisk") or 0) / 2**30
        else:
            st = item.get("statistics") or {}
            has_files = bool(st.get("episodeFileCount"))
            size = (st.get("sizeOnDisk") or 0) / 2**30
        cand = Candidate(kind, item.get("tmdbId") if kind == "movie" else item.get("tvdbId"), title,
                         item.get("year"), lib_id=item["id"])
        flow = Flow(uuid.uuid4().hex[:10], chat_id, cand, stage="remove")
        self.flows[flow.id] = flow
        if has_files:
            gb = f" ({size:.1f} GB)".replace(".", ",") if size else ""
            text = f"🗑 {title} löschen?\nEs sind Dateien vorhanden{gb}."
            buttons = [("🗑 Löschen inkl. Dateien", "files"), (f"📋 Nur aus {tool} entfernen", "keep")]
        else:
            text = f"🗑 {title} aus {tool} löschen? Laufende Downloads werden abgebrochen."
            buttons = [("🗑 Ja, löschen", "keep")]
        flow.step = Step(text, buttons + [("Abbrechen", "no")])
        return flow

    async def _do_remove(self, flow: Flow, value: str) -> Step:
        c = flow.cand
        if value not in ("files", "keep"):
            return Step(f"👍 {c.title} bleibt.", done=True)
        try:
            item = await self._svc(c.kind).get(c.lib_id)
        except ArrError:
            item = {}
        try:
            await self._svc(c.kind).delete(c.lib_id, delete_files=value == "files")
        except ArrError as e:
            return Step(f"❌ {c.title} konnte nicht gelöscht werden: {e}", done=True)
        self.store.forget(f"{'radarr' if c.kind == 'movie' else 'sonarr'}:{c.lib_id}")
        self._seen.get(flow.chat_id, {}).pop((c.kind, c.ext_id), None)
        if item:
            self._learn(flow.chat_id, "deleted", c.kind, item, c.title)
        what = " inkl. Dateien" if value == "files" else " (Dateien bleiben)"
        await self._added(flow.chat_id, f"🗑 {c.title} gelöscht{what}")
        return Step(f"🗑 {c.title} ist gelöscht{what}.", done=True)

    async def _t_remove_title(self, chat_id: int, kind: str, lib_id: int) -> Final:
        flow = await self.start_remove(chat_id, kind, int(lib_id))
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def pause(self, chat_id: int, kind: str, lib_id: int, days: int | None) -> str:
        item, key, title = await self._managed(chat_id, kind, lib_id, perm="pause")
        await self._svc(kind).set_monitored(item["id"], False)
        until = (utcnow() + timedelta(days=days)) if days else None
        entry = self.store.state.paused.get(key) or {"title": title, "chat_ids": []}
        if chat_id not in entry["chat_ids"]:
            entry["chat_ids"].append(chat_id)
        entry["until"] = until.isoformat() if until else None
        self.store.state.paused[key] = entry
        self.store.save()
        if until:
            when = "morgen" if days == 1 else f"am {until.astimezone(self.tz):%d.%m.}"
            return f"⏸ Suche nach {title} pausiert. Ich suche {when} wieder und sage dir dann Bescheid."
        return f"⏸ Suche nach {title} pausiert. Sag „weiter mit {item.get('title')}“, wenn ich wieder suchen soll."

    async def resume(self, chat_id: int, kind: str, lib_id: int) -> str:
        item, key, title = await self._managed(chat_id, kind, lib_id, perm="pause")
        await resume_search(self._svc(kind), item["id"])
        entry = self.store.state.paused.pop(key, None)
        for chat in (entry or {}).get("chat_ids") or [chat_id]:
            self.store.track("radarr" if kind == "movie" else "sonarr", item["id"], title, chat)
        return f"▶️ Suche nach {title} läuft wieder. Ich melde mich, wenn der Download startet."

    async def _t_pause_title(self, chat_id: int, kind: str, lib_id: int, days: int | None = None) -> Final:
        return Final(await self.pause(chat_id, kind, int(lib_id), int(days) if days else None))

    async def _t_resume_title(self, chat_id: int, kind: str, lib_id: int) -> Final:
        return Final(await self.resume(chat_id, kind, int(lib_id)))

    async def _t_list_quality_profiles(self, chat_id: int, kind: str) -> list[str]:
        return [p["name"] for p in await self.allowed_profiles(kind, chat_id)]

    async def _t_download_status(self, chat_id: int, finished: bool = False, title: str | None = None) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "downloads", bool(finished), title or None))
        return Final("")

    # ---- notifications and bot info ----------------------------------------------------------

    async def _t_mute_notifications(self, chat_id: int, minutes: int | None = None, off: bool = False,
                                    hours: int | None = None, days: int | None = None) -> Final:
        total = (minutes or 0) + (hours or 0) * 60 + (days or 0) * 1440
        return Final(mute(self.store, chat_id, None if off else (total or 60)))

    async def _t_notification_settings(self, chat_id: int, start: bool | None = None, done: bool | None = None,
                                       problem: bool | None = None, nothing: bool | None = None) -> Final:
        from . import notify

        notify.change(self.store, chat_id, start=start, done=done, problem=problem, nothing=nothing)
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "notify"))
        return Final("")

    async def _t_bot_info(self, chat_id: int) -> Final:
        if self.bot_info is None:
            raise ArrError("Keine Versionsinfo verfügbar.")
        return Final(await self.bot_info(chat_id))

    # ---- who may press a button ---------------------------------------------------------------
    def owner_ok(self, kind: str, obj_id: str, uid: int, chat_id: int) -> bool:
        """A button may only be pressed in the chat it was sent to, by someone allowed there. Unknown ids
        (expired) are fine: the handlers answer "abgelaufen" themselves."""
        obj = (self.flows if kind == "flow" else self.offers).get(obj_id)
        if obj is None:
            return True
        if obj.chat_id != chat_id:
            return False
        stage = getattr(obj, "stage", "")
        if stage in ("mm", "dup"):  # media manager: the right is needed again at the moment of deleting
            return self.may(uid, "admin_delete_media")
        if stage == "remove":
            return self.may(uid, "remove_title")
        if stage == "wa":
            return self.may(uid, "admin_delete_media")
        if stage == "adv":
            return self._admin(uid) and self.users is not None and self.is_admin(uid)
        return True

    # ---- calendar, disk space, weekly digest, links --------------------------------------------

    async def _t_calendar(self, chat_id: int, days: int | None = None, mine: bool = False) -> Final:
        days = max(1, min(int(days or 7), 31))
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "calendar", arg=(days, bool(mine))))
        return Final("")

    async def _t_disk_space(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "disk"))
        return Final("")

    async def _t_weekly_digest(self, chat_id: int, day: str | None = None, off: bool = False) -> Final:
        if not self.profiles:
            return Final("Profile sind nicht eingerichtet.")
        if off:
            self.profiles.set_digest(chat_id, None)
            return Final("🔕 Okay, kein Wochenrückblick mehr.")
        n = watch.parse_day(day) if day is not None else 6
        if n is None:
            return Final("An welchem Wochentag? Z. B. „jeden Sonntag“.")
        self.profiles.set_digest(chat_id, n)
        return Final(f"📰 Gern! Du bekommst jeden {profiles.DAY_NAMES[n]} um 10 Uhr deinen Wochenrückblick: "
                     "was fertig wurde, was in der Woche kommt, und ein paar Vorschläge.")

    async def _t_open_url(self, chat_id: int, url: str) -> dict:
        try:
            info = await urlinfo.identify(url, **self.url_options)
        except urlinfo.UrlError as e:
            raise ArrError(str(e)) from e
        info.pop("description", None)  # not needed to find the title; saves tokens
        return _compact(info)
