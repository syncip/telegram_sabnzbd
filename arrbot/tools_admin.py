"""Admin tools: users, limits, rights, stats, quality profiles, approvals, recipes.

A mixin of ToolRunner (arrbot/tools.py): self is the ToolRunner."""

from __future__ import annotations

from dataclasses import asdict
import logging
import uuid

from . import qprofiles, recipes, users as users_mod
from .arr import ArrError
from .state import utcnow
from .tmdb import TmdbError
from .tooltypes import Candidate, Final, Flow, Step, View

log = logging.getLogger(__name__)


class AdminTools:
    async def _t_admin_quality_profile(self, chat_id: int, name: str, service: str | None = None,
                                       min_resolution: int | None = None, max_resolution: int | None = None,
                                       required_languages: list[str] | None = None,
                                       preferred_languages: list[str] | None = None, only: bool = False) -> Final:
        w = qprofiles.make_wish(name, service, min_resolution, max_resolution, required_languages,
                                preferred_languages, only)
        w.services = [s for s in w.services if (self.radarr if s == "radarr" else self.sonarr)]
        if not w.services:
            raise ArrError("Dafür ist weder Radarr noch Sonarr eingerichtet.")
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, w.name, None), stage="qprof", data=w)
        flow.step = Step(w.summary() + "\n\nSo anlegen?", [("✅ Anlegen", "yes"), ("Abbrechen", "no")])
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _create_profiles(self, flow: Flow, value: str) -> Step:
        w = flow.data
        if value != "yes":
            return Step("👍 Okay, kein neues Profil.", done=True)
        lines = []
        for s in w.services:
            svc = self.radarr if s == "radarr" else self.sonarr
            try:
                lines.append(await qprofiles.create(svc, w))
            except ArrError as e:
                lines.append(f"❌ {svc.kind}: {e}")
        lines.append(f"Nutzen: „Lad Dune in {w.name}“ oder als Standard über RADARR_QUALITY_PROFILE / "
                     "SONARR_QUALITY_PROFILE.")
        return Step("\n".join(lines), done=True)

    async def _t_admin_media(self, chat_id: int, section: str = "overview", days: int | None = None) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "media", arg=(section, days)))
        return Final("")

    async def _t_admin_approval(self, chat_id: int, action: str, user: str | None = None, kind: str = "all",
                                genres: list[str] | None = None, min_gb: float | None = None,
                                rule_id: int | None = None) -> Final:
        if self.approvals is None:
            raise ArrError("Freigaben sind nicht eingerichtet.")
        if action == "add":
            who = self.users.find(user) if user and user.lower() not in ("alle", "allen", "jeder", "all") else None
            if user and who is None and user.lower() not in ("alle", "allen", "jeder", "all"):
                raise ArrError(f"Nutzer {user} nicht gefunden.")
            rule = self.approvals.add(who, kind or "all", genres, min_gb)
            return Final(f"✋ Regel {rule.id}: {rule.text(self.users.name)} – erst nach deiner Freigabe.")
        if action == "remove":
            if rule_id is None or not self.approvals.remove(int(rule_id)):
                raise ArrError(f"Regel {rule_id} gibt es nicht.")
            return Final(f"🗑 Freigabe-Regel {rule_id} gelöscht.")
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "approvals"))
        return Final("")

    async def _t_admin_activity(self, chat_id: int, user: str | None = None, problems_only: bool = False,
                                days: int = 7) -> Final:
        who = self.users.find(user) if user else None
        if user and who is None:
            raise ArrError(f"Nutzer {user} nicht gefunden.")
        mode = "problems" if problems_only else "all"
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "activity", arg=(mode, max(1, min(days, 90)), who)))
        return Final("")

    async def _t_admin_stats(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "stats"))
        return Final("")

    async def _t_admin_users(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "users"))
        return Final("")

    async def _t_admin_check(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "check"))
        return Final("")

    async def _t_admin_set_limit(self, chat_id: int, who: str, kind: str, value: str | float | None = None) -> Final:
        try:
            return Final(self.users.apply_limit(who, kind, value))
        except ValueError as e:
            raise ArrError(str(e)) from e

    @staticmethod
    def _recipe_cost_text(cost: float | None, tokens: int) -> str:
        money = f" ≈ {cost * 100:.2f} ct".replace(".", ",") if cost is not None else ""
        return f"{tokens:,} Tokens".replace(",", ".") + money

    async def _t_build_tool(self, chat_id: int, request: str) -> Final:
        if self.recipes is None or self.builder is None:
            raise ArrError("Werkzeuge bauen ist nicht eingerichtet.")
        inp, out = recipes.estimate(request)
        cost = self.builder_cost({"input": inp, "output": out})
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Werkzeug", None), stage="recipe",
                    data={"request": request})
        money = f" ≈ {cost * 100:.1f} ct".replace(".", ",") if cost is not None else ""
        tokens = f"{inp + out:,}".replace(",", ".")
        flow.step = Step(
            f"🛠 Dafür habe ich noch kein Werkzeug:\n„{request}“\n\n"
            f"Ich kann mir eins bauen lassen ({self.builder_model}): etwa {tokens} Tokens{money}, "
            "mit Korrekturversuch bis doppelt so viel. Danach kostet jede Nutzung nur die normale Frage.\n"
            "Das Werkzeug kann nur lesen (TMDB, Radarr, Sonarr). Du siehst einen Probelauf und entscheidest, "
            "ob es bleibt.",
            [(f"🛠 Bauen{money}", "build"), ("Abbrechen", "no")])
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    def _recipe_clients(self) -> dict:
        return {k: v for k, v in (("tmdb", self.tmdb), ("radarr", self.radarr), ("sonarr", self.sonarr)) if v}

    async def _recipe_step(self, flow: Flow, value: str) -> Step:
        d = flow.data
        if value == "keep" and d.get("recipe"):
            r = d["recipe"]
            r.created, r.tokens, r.cost = utcnow().isoformat(), d["tokens"], d["cost"] or 0.0
            self.recipes.save(r)  # type: ignore[union-attr]
            return Step(f"✅ Werkzeug „{r.title}“ gespeichert. Frag einfach wieder so, dann nehme ich es. "
                        "/rezepte zeigt alle.", done=True)
        if value != "build" or d.get("recipe"):
            return Step("👍 Okay, kein neues Werkzeug.", done=True)
        usage = {"input": 0, "cached": 0, "output": 0}
        error, raw, recipe, outcome = None, None, None, None
        for _ in range(2):  # one try to correct itself
            try:
                recipe, example, u = await recipes.build(self.builder, d["request"], error, raw)
                for k in usage:
                    usage[k] += u.get(k, 0) or 0
                raw = asdict(recipe)
                outcome = await recipes.run(recipe, example, self._recipe_clients())
                if outcome.lines:
                    break
                error = "Der Probelauf hat nichts gefunden – Filter, Pfad oder items prüfen."
            except recipes.RecipeError as e:
                for k in usage:
                    usage[k] += (getattr(e, "usage", None) or {}).get(k, 0) or 0
                raw = getattr(e, "raw", raw)
                error, recipe, outcome = str(e), None, None
            except (ArrError, TmdbError) as e:
                error, outcome = f"Abfrage fehlgeschlagen: {e}", None
            except Exception as e:  # noqa: BLE001 - the model gave something odd
                log.exception("Rezept bauen")
                error, outcome = f"{type(e).__name__}: {e}", None
        tokens = usage["input"] + usage["output"]
        cost = self.builder_cost(usage)
        if self.on_llm_usage and tokens:
            self.on_llm_usage(flow.chat_id, usage, cost)
        spent = self._recipe_cost_text(cost, tokens)
        if recipe is None or outcome is None or not outcome.lines:
            return Step(f"❌ Das Werkzeug hat nicht geklappt: {error}\n💰 Verbraucht: {spent}", done=True)
        d.update(recipe=recipe, tokens=tokens, cost=cost)
        preview = "\n".join(outcome.lines[:8]) + ("\n…" if len(outcome.lines) > 8 else "")
        params = ", ".join(recipe.params) or "keine"
        return Step(f"🛠 Neues Werkzeug: {recipe.title}\n{recipe.description}\nEinstellbar: {params}\n\n"
                    f"Probelauf:\n{outcome.heading}\n{preview}\n\n💰 Bau: {spent}",
                    [("✅ Behalten", "keep"), ("🗑 Verwerfen", "no")])

    async def run_recipe(self, chat_id: int, name: str, args: dict) -> Final:
        r = self.recipes.get(name) if self.recipes else None
        if r is None:
            raise ArrError("Dieses Werkzeug gibt es nicht mehr.")
        try:
            out = await recipes.run(r, args, self._recipe_clients())
        except recipes.RecipeError as e:
            raise ArrError(str(e)) from e
        r.uses += 1
        self.recipes.save(r)  # type: ignore[union-attr]
        if not out.lines:
            return Final(f"{out.heading}\n🤷 Nichts gefunden.")
        text = "\n".join([out.heading, *[f"{i}. {ln}" for i, ln in enumerate(out.lines, 1)]])
        cands = await self._recipe_cands(r, out.items) if r.offer else []
        if cands:
            self._offer(chat_id, cands, text + "\n\nTippe einen Titel an, um ihn hinzuzufügen.", bulk=True)
            return Final("")
        return Final(text)

    async def _recipe_cands(self, r: recipes.Recipe, items: list) -> list[Candidate]:
        svc = self.radarr if r.offer == "movie" else self.sonarr
        owned = await self._library_ids([r.offer]) if svc else None
        out = []
        for i in items:
            tid = recipes.dig(i, r.id_field)
            if not isinstance(tid, int):
                continue
            title = recipes.dig(i, "title") or recipes.dig(i, "name") or "?"
            day = str(recipes.dig(i, "release_date") or recipes.dig(i, "first_air_date") or "")
            year = int(day[:4]) if day[:4].isdigit() else None
            if r.offer == "movie":
                if owned and ("movie", tid) in owned["library"]:
                    continue
                out.append(Candidate("movie", tid, title, year, tmdb_id=tid))
            elif self.tmdb:
                try:
                    tvdb = await self.tmdb.tvdb_from_tv(tid)
                except TmdbError:
                    continue
                if tvdb and not (owned and tvdb in owned["tvdb"]):
                    out.append(Candidate("series", tvdb, title, year, tmdb_id=tid))
        return out

    async def _t_admin_recipes(self, chat_id: int, action: str = "list", name: str | None = None) -> Final:
        if self.recipes is None:
            raise ArrError("Werkzeuge bauen ist nicht eingerichtet.")
        if action == "delete":
            want = (name or "").lower().removeprefix("recipe_")
            r = next((x for x in self.recipes.all() if want in (x.name, x.title.lower())), None)
            if r is None or not self.recipes.remove(r.name):
                raise ArrError(f"Werkzeug „{name}“ gibt es nicht.")
            return Final(f"🗑 Werkzeug „{r.title}“ gelöscht.")
        lines = [f"• {r.title} ({r.tool}) – {r.uses}× benutzt" for r in self.recipes.all()]
        return Final("🛠 Selbstgebaute Werkzeuge:\n" + "\n".join(lines) if lines
                     else "🛠 Noch keine selbstgebauten Werkzeuge.")

    async def _t_admin_overview(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "admin"))
        return Final("")

    async def _t_admin_user_info(self, chat_id: int, who: str) -> Final:
        uid = self.users.find(str(who)) if self.users else None
        if uid is None or uid not in self.users.users:
            raise ArrError(f"Nutzer „{who}“ kenne ich nicht. /nutzer zeigt alle.")
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "usercard", arg=uid))
        return Final("")

    async def _t_admin_limit_advice(self, chat_id: int) -> Final:
        from . import limitadvice

        a = limitadvice.advise(self.users)
        text = limitadvice.render(a, self.users)
        if not a.total:
            return Final(text)
        buttons = [("✅ Alles übernehmen", "all"), ("Nur Gesamt", "total")]
        if a.standard:
            buttons.append(("Nur Standard pro Nutzer", "standard"))
        buttons.append(("✖️ Nein danke", "no"))
        flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate("movie", 0, "Limits", None), stage="adv", data=a)
        flow.step = Step(text, buttons)
        self.flows[flow.id] = flow
        self._outbox.setdefault(chat_id, []).append(flow)
        return Final("")

    async def _t_admin_permissions(self, chat_id: int, who: str, grant: list[str] | None = None,
                                   revoke: list[str] | None = None) -> Final:
        try:
            text = self.users.apply_perms(who, grant, revoke)
        except ValueError as e:
            raise ArrError(str(e)) from e
        uid = self.users.find(who)
        if (grant or revoke) and uid and self.notify_user and uid != chat_id:
            u = self.users.users.get(uid)
            if u is not None:
                await self.notify_user(uid, users_mod.perms_text(u).replace(f"🔐 {u.name} darf", "🔐 Du darfst"))
        return Final(text)

    async def _t_admin_max_quality(self, chat_id: int, who: str, quality: str) -> Final:
        try:
            return Final(self.users.apply_quality(who, quality))
        except ValueError as e:
            raise ArrError(str(e)) from e

    async def _t_admin_user(self, chat_id: int, who: str, action: str) -> Final:
        try:
            uid, text = self.users.apply_role(who, action)
        except ValueError as e:
            raise ArrError(str(e)) from e
        if self.notify_user and action == "freigeben":
            await self.notify_user(uid, "🎉 Du bist freigeschaltet! Schreib mir einfach, was du sehen willst.")
        return Final(text)
