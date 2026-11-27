"""Suggestions, top lists, taste profile, feedback, wishlist, watched sites.

A mixin of ToolRunner (arrbot/tools.py): self is the ToolRunner."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
import logging
from typing import Any
import uuid

from . import plex, profiles, recommend, sitewatch, together, urlinfo, wishlist
from .arr import ArrError
from .tmdb import TmdbError
from .tooltypes import Candidate, Final, Flow, Held, MAX_SUGGESTIONS, MAX_TOP, MONTHS, NO_TMDB, Offer, View

log = logging.getLogger(__name__)


class DiscoverTools:
    async def _t_suggest_together(self, chat_id: int, with_user: str, kind: str | None = None) -> Final:
        if self.profiles is None or self.users is None:
            raise ArrError("Gemeinsame Vorschläge brauchen Nutzerprofile.")
        other = self.users.find(with_user)
        if other is None or not self.users.is_allowed(other):
            raise ArrError(f"„{with_user}“ kenne ich nicht – der Name muss so heißen wie in der Nutzerliste.")
        if other == chat_id:
            raise ArrError("Das bist du selbst. 🙂 Mit wem willst du schauen?")
        a, b = self.profiles.profile(chat_id), self.profiles.profile(other)
        kinds = [kind] if kind else ["movie", "series"]

        here: list[together.Pick] = []
        acc_a = self.plex_store.get(chat_id) if self.plex_store else None
        acc_b = self.plex_store.get(other) if self.plex_store else None
        if self.plex and (acc_a or acc_b):
            try:
                main, extra = (acc_a, acc_b) if acc_a else (acc_b, None)
                items_main = await self.plex.library(main)
                items_extra = await self.plex.library(extra) if extra else None
                here = [p for p in together.from_library(items_main, items_extra, a, b) if p.kind in kinds][:6]
            except plex.PlexError as e:
                log.info("Plex für gemeinsame Vorschläge: %s", e)

        cands: list[Candidate] = []
        if self.tmdb:
            owned = await self._library_ids([k for k in kinds if (self.radarr if k == "movie" else self.sonarr)])
            seeds = [s for s in self.profiles.recent(chat_id, 5) + self.profiles.recent(other, 5)
                     if s.get("tmdb") and s.get("kind") in kinds]
            pool: dict[tuple[str, int], tuple[float, Any]] = {}
            results = await asyncio.gather(*(self.tmdb.recommendations(s["kind"], s["tmdb"]) for s in seeds),
                                           return_exceptions=True)
            for s, res in zip(seeds, results):
                if isinstance(res, BaseException):
                    continue
                for rank, r in enumerate(res[:20]):
                    key = (s["kind"], r.tmdb_id)
                    if key in owned["tmdb"] or together.excluded(a, b, *key):
                        continue
                    score = together.fit(a, b, r.genres, r.rating)
                    if score is None:
                        continue
                    old = pool.get(key, (0.0, r))[0]
                    pool[key] = (old + score + (20 - rank) / 40, r)  # recommended for both: adds up
            best = sorted(pool.items(), key=lambda kv: -kv[1][0])[:5]
            for (k, tid), (_, r) in best:
                if k == "movie":
                    cands.append(Candidate("movie", r.tmdb_id, r.title, r.year, rating=r.rating, tmdb_id=r.tmdb_id,
                                           genres=r.genres))
                else:
                    tvdb = await self.tmdb.tvdb_from_tv(r.tmdb_id)
                    if tvdb:
                        cands.append(Candidate("series", tvdb, r.title, r.year, rating=r.rating, tmdb_id=r.tmdb_id,
                                               genres=r.genres))
        text = together.render(self.users.name(chat_id), self.users.name(other), here, len(cands))
        if cands:
            self._offer(chat_id, cands, "➕ Neu für euch:", suggest=True)
        return Final(text)

    async def _t_wishlist(self, chat_id: int, action: str, kind: str | None = None, id: int | None = None,  # noqa: A002
                          wish_id: int | None = None) -> Final:
        if self.wishes is None:
            raise ArrError("Die Wunschliste ist nicht eingerichtet.")
        if action == "remove":
            if wish_id is None or not self.wishes.remove(int(wish_id), chat_id):
                raise ArrError(f"Wunsch {wish_id} gibt es nicht.")
            return Final(f"🗑 Wunsch {wish_id} gestrichen.")
        if action != "add":
            return Final(wishlist.render(self.wishes.wishes(chat_id)))
        if kind not in ("movie", "series") or not id:
            raise ArrError("Erst den Titel suchen, dann mit seiner ID vormerken.")
        svc = self._need(self.radarr if kind == "movie" else self.sonarr, "Radarr" if kind == "movie" else "Sonarr")
        item = await (svc.lookup_tmdb(int(id)) if kind == "movie" else svc.lookup_tvdb(int(id)))
        title = item.get("title") or "?"
        name = f"{title} ({item['year']})" if item.get("year") else title
        if item.get("id"):
            return Final(f"👍 {name} ist schon da.")
        when, what = wishlist.release_date(kind, item)
        if when and when <= date.today():
            cand = await self._candidate(chat_id, kind, int(id))
            self._offer(chat_id, [cand], f"📅 {name} ist schon erschienen ({what} {when:%d.%m.%Y}). Gleich laden?")
            return Final("")
        if when:  # there is a date already: add it now, it loads on its own once released
            flow = Flow(uuid.uuid4().hex[:10], chat_id, Candidate(kind, int(id), title, item.get("year")), monitor="all")
            try:
                await self._add(flow)
            except Held:
                return Final(f"📅 {name} kommt am {when:%d.%m.%Y} ({what}). Der Wunsch wartet auf die Freigabe.")
            return Final(f"📅 {name} hat schon einen Termin ({what} {when:%d.%m.%Y}). Ich habe es angelegt – "
                         "es lädt von selbst, sobald es verfügbar ist.")
        if not self.wishes.add(chat_id, kind, int(id), name):
            return Final(f"📌 {name} steht schon auf deiner Wunschliste.")
        return Final(f"📌 {name} ist gemerkt. Noch gibt es keinen Termin – sobald es einen gibt, lege ich es an "
                     "und sage dir Bescheid.")

    async def _t_watch_site(self, chat_id: int, action: str, url: str | None = None, kind: str = "all",
                            site_id: int | None = None) -> Final:
        if self.sites is None:
            raise ArrError("Seiten-Beobachtung ist nicht eingerichtet.")
        if action == "add":
            if not url:
                raise ArrError("Welche Seite soll ich beobachten? Schick mir den Link.")
            url = url.strip() if "://" in url else "https://" + url.strip()
            mine = self.sites.sites(chat_id)
            if any(s.url == url for s in mine):
                return Final("👀 Die Seite beobachte ich schon für dich.")
            if len(mine) >= sitewatch.MAX_SITES_PER_USER and not self.is_admin(chat_id):
                raise ArrError(f"Du beobachtest schon {len(mine)} Seiten – lösch erst eine („Beobachtete Seiten“).")
            try:
                final_url, page = await urlinfo.fetch(url)
            except urlinfo.UrlError as e:
                raise ArrError(f"Seite nicht lesbar: {e}") from e
            lines = sitewatch.page_lines(page)
            if len(lines) < 5:
                raise ArrError("Auf der Seite steht kaum Text (lädt sie erst per JavaScript?). Die kann ich nicht beobachten.")
            site = self.sites.add(chat_id, final_url, kind or "all", lines)
            what = {"movie": "Filme", "series": "Serien"}.get(site.kind, "Filme und Serien")
            return Final(f"👀 Ich beobachte {site.host} auf neue {what} und schaue alle {self.site_hours} Std. nach. "
                         "Nur was neu dazukommt, zeige ich dir – mit Buttons zum Hinzufügen. "
                         "„Nichts davon“ merke ich mir für bessere Vorschläge.")
        if action == "remove":
            mine = None if self.is_admin(chat_id) else chat_id
            if site_id is None or not self.sites.remove(int(site_id), mine):
                raise ArrError(f"Seite {site_id} gibt es nicht.")
            return Final(f"🗑 Seite {site_id} wird nicht mehr beobachtet.")
        return Final(sitewatch.render_sites(self.sites.sites(chat_id)))

    async def check_site(self, site: sitewatch.Site, provider: Any) -> dict:
        """One look at a watched page. New titles go to the user as buttons. Returns the AI usage."""
        _, page = await urlinfo.fetch(site.url)
        lines = sitewatch.page_lines(page)
        if sitewatch.digest(lines) == site.digest:
            self.sites.touch(site)  # type: ignore[union-attr]
            return {}
        fresh = sitewatch.new_lines(site, lines)
        if not fresh:
            self.sites.update(site, lines, 0)  # type: ignore[union-attr]
            return {}
        items, usage = await sitewatch.extract(provider, fresh)
        found = await sitewatch.resolve(items, site.kind, self.radarr, self.sonarr, profiles.genre_de)
        prof = self.profiles.profile(site.user_id) if self.profiles else None
        avoid = {g.lower() for g in (prof.prefs.avoid if prof else [])}
        cands = []
        for f in found:
            if f["have"] or {g.lower() for g in f["genres"]} & avoid:
                continue
            if prof and f["tmdb_id"] and ((f["kind"], f["tmdb_id"]) in prof.rejected or (f["kind"], f["tmdb_id"]) in prof.seen):
                continue
            cands.append(Candidate(f["kind"], f["ext_id"], f["title"], f["year"], tmdb_id=f["tmdb_id"],
                                   genres=f["genres"], rating=0.0))
        if prof:
            cands.sort(key=lambda c: -prof.genre_bonus(c.genres))
        self.sites.update(site, lines, len(cands))  # type: ignore[union-attr]
        if cands:
            self._offer(site.user_id, cands[:sitewatch.MAX_OFFER], f"🌐 Neu auf {site.host}:", bulk=len(cands) > 1,
                        suggest=True)
        return usage

    async def _t_suggest(self, chat_id: int, kind: str | None = None, from_year: int | None = None,
                         to_year: int | None = None, min_rating: float | None = None, mood: str | None = None,
                         genres: list[str] | None = None, like: str | None = None, max_minutes: int | None = None,
                         german_only: bool = False, calibrate: bool = False) -> Final | dict:
        wanted = recommend.wanted_genres(mood, genres)

        def fits(r: Any) -> bool:
            if (r.kind, r.tmdb_id) in skip or avoid.intersection(r.genres):
                return False
            if wanted and not wanted.intersection(r.genres):
                return False
            if (from_year or to_year) and not r.year:
                return False
            return ((not from_year or r.year >= int(from_year)) and (not to_year or r.year <= int(to_year))
                    and (not min_rating or r.rating >= float(min_rating)))

        def wish_text() -> str:
            return " ".join(x for x in (
                (mood or "").strip() or ", ".join(sorted(wanted)),
                f"ab {from_year}" if from_year and not to_year else "",
                f"bis {to_year}" if to_year and not from_year else "",
                f"{from_year}–{to_year}" if from_year and to_year else "",
                f"ab ⭐ {float(min_rating):g}".replace(".", ",") if min_rating else "",
                f"bis {int(max_minutes)} Min." if max_minutes else "",
                "auf Deutsch" if german_only else "") if x)

        kinds = [kind] if kind else [k for k, svc in (("movie", self.radarr), ("series", self.sonarr)) if svc]
        history = [r for r in reversed(self.store.state.requests) if r["chat_id"] == chat_id and r["kind"] in kinds]
        if not self.tmdb:
            # Without TMDB the model suggests from its own knowledge, then searches as usual.
            return {
                "bisher": [r["title"] for r in history[:15]] or "nichts",
                "hinweis": "Nenne 3-5 passende Titel (deutsche Titel), suche sie und biete sie per offer_titles an."
                + (f" Nur: {wish_text()}." if wish_text() else "") + (f" Ähnlich wie: {like}." if like else ""),
            }
        prof = self.profiles.profile(chat_id) if self.profiles else None
        explicit = bool(wanted or from_year or to_year or min_rating or max_minutes or german_only or like)
        if prof:  # defaults the user told the bot once ("nur Filme ab 2000")
            from_year = from_year or prof.prefs.from_year
            min_rating = min_rating or prof.prefs.min_rating
        avoid = set(prof.prefs.avoid) if prof else set()
        skip = (prof.rejected | prof.seen) if prof else set()
        owned = await self._library_ids(kinds)

        like_title = None
        if like:
            hit = await self._resolve_title(kind, like)
            if hit is None:
                return Final(f"🤷 „{like}“ habe ich nicht gefunden – wie heißt der Titel genau (Film oder Serie)?")
            like_title = hit["title"]
            kinds = [hit["kind"]]
            seeds = [(hit["kind"], hit["tmdb"], hit["title"])]
        else:
            personal = ([(k, t, title) for k, t, title, _ in self.profiles.seeds(chat_id, 10) if k in kinds]
                        if self.profiles else [])
            if not personal:
                personal = [(r["kind"], r.get("tmdb"), r["title"]) for r in history if r.get("tmdb")]
            if self.profiles and (calibrate or (not personal and not explicit
                                                and not self.profiles.asked(chat_id))):
                return await self._calibrate(chat_id, kinds)
            seeds = list(dict.fromkeys(personal))[:8]
            if len(seeds) < 3:  # little history: the shared library shows the household's taste
                seeds += [s for s in owned["recent"] if s not in seeds][: 8 - len(seeds)]

        pool: dict[tuple[str, int], Any] = {}
        on_server: dict[tuple[str, int], Any] = {}  # on the server, but this user has not seen it yet
        results = await asyncio.gather(
            *(self.tmdb.recommendations(k, tid) for k, tid, _ in seeds), return_exceptions=True
        )
        for (k, _tid, title), res in zip(seeds, results):
            if isinstance(res, BaseException):
                continue
            for rank, r in enumerate(res[:20]):
                key = (k, r.tmdb_id)
                if key in owned["tmdb"]:
                    if key in owned["server"] and fits(r):
                        tip = on_server.setdefault(key, r)
                        tip.score += (20 - rank) / 20
                        if title not in tip.because:
                            tip.because.append(title)
                    continue
                if not fits(r):
                    continue
                cur = pool.setdefault(key, r)
                cur.score += (20 - rank) / 20
                if title not in cur.because:
                    cur.because.append(title)
        if like_title and pool:
            intro = f"💡 Wie {like_title}:"
        elif pool and (len(pool) >= 3 or not explicit):
            reasons = list(dict.fromkeys(t for _, _, t in seeds))[:3]
            intro = f"💡 Das könnte dir gefallen – weil du {', '.join(reasons)} magst:"
        elif wanted or max_minutes or min_rating:
            # The recommendations have too little of it: well rated titles of the wanted genres.
            for k in kinds:
                try:
                    found = await self.tmdb.discover(k, recommend.genre_ids(k, wanted), max_minutes,
                                                     from_year, to_year, min_rating)
                except TmdbError:
                    continue
                for r in found[:20]:
                    if (k, r.tmdb_id) not in owned["tmdb"] and fits(r):
                        pool.setdefault((k, r.tmdb_id), r)
            intro = "🎯 Passend zu deinem Wunsch:"
        elif from_year or to_year:
            # Too few matches in the recommendations: the best rated titles of the wanted years.
            start = date(int(from_year or 1900), 1, 1)
            end = date(int(to_year), 12, 31) if to_year else date.today()
            for k in kinds:
                for r in (await self.tmdb.top(k, start, end, "rating"))[:20]:
                    if (k, r.tmdb_id) not in owned["tmdb"] and fits(r):
                        pool.setdefault((k, r.tmdb_id), r)
            intro = "🏆 Gut bewertet aus der Zeit:"
        else:
            for k in kinds:
                for r in (await self.tmdb.trending(k))[:10]:
                    if (k, r.tmdb_id) not in owned["tmdb"] and fits(r):
                        pool.setdefault((k, r.tmdb_id), r)
            intro = "🔥 Gerade beliebt in Deutschland:"
        if wish_text():
            intro = intro.rstrip(":") + f" ({wish_text()}):"
        ranked = sorted(pool.values(), key=lambda r: recommend.score(r, prof.genre_bonus(r.genres) if prof else 0.0),
                        reverse=True)

        # Runtime and film series of the best ones: "unter 90 Minuten", not three parts of one series.
        collections: dict[tuple[str, int], int] = {}
        top = ranked[:16]
        infos = await asyncio.gather(*(self.tmdb.details(r.kind, r.tmdb_id) for r in top), return_exceptions=True)
        too_long = set()
        for r, info in zip(top, infos):
            if isinstance(info, BaseException):
                continue
            if info.get("collection"):
                collections[(r.kind, r.tmdb_id)] = info["collection"]
            if max_minutes and info.get("runtime") and info["runtime"] > int(max_minutes):
                too_long.add((r.kind, r.tmdb_id))
        ranked = [r for r in ranked if (r.kind, r.tmdb_id) not in too_long]
        order = recommend.diversify(ranked, MAX_SUGGESTIONS, collections)
        order += [r for r in ranked if r not in order]

        cands: list[Candidate] = []
        lines: list[str] = []
        checked = 0
        for r in order:
            if len(cands) >= MAX_SUGGESTIONS:
                break
            if german_only:
                if checked >= 18:
                    break
                checked += 1
                try:
                    if not recommend.german_available(await self.tmdb.german_info(r.kind, r.tmdb_id)):
                        continue
                except TmdbError:
                    continue
            if r.kind == "movie":
                c = Candidate("movie", r.tmdb_id, r.title, r.year)
            else:
                try:
                    tvdb = await self.tmdb.tvdb_from_tv(r.tmdb_id)
                except TmdbError:
                    continue
                if not tvdb or tvdb in owned["tvdb"]:
                    continue
                c = Candidate("series", tvdb, r.title, r.year)
            c.rating = r.rating
            c.tmdb_id, c.genres = r.tmdb_id, r.genres  # for "nichts davon" / 👎
            self._remember(chat_id, c)
            cands.append(c)
            why = recommend.reason(r) if not like_title else ("💎 Geheimtipp" if "💎 Geheimtipp" in r.because else "")
            if why:
                lines.append(f"• {r.title}: {why}")
        tips = sorted(on_server.values(), key=lambda r: recommend.score(r), reverse=True)[:2]
        tip_lines = [f"📼 Schon auf dem Server: {r.title}" + (f" ({r.year})" if r.year else "")
                     + (f" – wie {r.because[0]}" if r.because else "") for r in tips]
        if not cands:
            if tip_lines:
                return Final("Neues habe ich gerade nicht, aber:\n" + "\n".join(tip_lines))
            if explicit:
                return Final(f"🤷 Dazu ({wish_text() or like}) finde ich gerade nichts Neues. "
                             "Magst du den Wunsch etwas lockern?")
            return Final("🤷 Mir fällt gerade nichts Neues ein – ihr habt schon alles Passende. 😄")
        text = "\n".join([intro, *lines, *([""] + tip_lines if tip_lines else []), "",
                          "Tippe einen Titel an, um ihn hinzuzufügen. 👎/👁 unter „Bewerten“."])
        self._offer(chat_id, cands, text, suggest=True)
        return Final("")

    async def _calibrate(self, chat_id: int, kinds: list[str]) -> Final:
        """Quick start for someone the bot knows nothing about: well known titles to 👍/👎."""
        pool: list[Any] = []
        for k in kinds:
            try:
                pool += (await self.tmdb.trending(k))[:15]
            except TmdbError:
                continue
        pool.sort(key=lambda r: r.votes, reverse=True)
        cands: list[Candidate] = []
        for r in recommend.diversify(pool, 8):
            if r.kind == "movie":
                c = Candidate("movie", r.tmdb_id, r.title, r.year)
            else:
                try:
                    tvdb = await self.tmdb.tvdb_from_tv(r.tmdb_id)
                except TmdbError:
                    continue
                if not tvdb:
                    continue
                c = Candidate("series", tvdb, r.title, r.year)
            c.tmdb_id, c.genres, c.rating = r.tmdb_id, r.genres, r.rating
            cands.append(c)
            self._learn_cand(chat_id, "asked", c)  # shown once; never again as quick start
        if not cands:
            return Final("🤷 Gerade bekomme ich keine Titel von TMDB. Versuch es später nochmal.")
        offer = self._offer(chat_id, cands, "👋 Kurz zum Kennenlernen, damit meine Tipps passen:\n"
                            "Tippe 👍 bei Titeln, die dir gefallen haben, 👎 bei denen, die nicht dein Ding "
                            "sind. Unbekannte einfach auslassen – dann „Fertig“.", suggest=True)
        offer.calibrate = True
        return Final("")

    async def _resolve_title(self, kind: str | None, title: str) -> dict | None:
        """A title the user named ("wie Inception") -> kind, tmdb id, German title, genres."""
        kinds = [kind] if kind else ["movie", "series"]
        best = None
        for k in kinds:
            svc = self.radarr if k == "movie" else self.sonarr
            if not svc:
                continue
            try:
                res = await svc.lookup(title)
            except ArrError:
                continue
            res = [x for x in res if x.get("tmdbId")]
            if not res:
                continue
            exact = next((x for x in res if self._norm(x.get("title") or "") == self._norm(title)), None)
            hit = exact or res[0]
            item = {"kind": k, "tmdb": hit["tmdbId"], "tvdb": hit.get("tvdbId") if k == "series" else None,
                    "title": hit.get("title") or title, "year": hit.get("year") or None,
                    "genres": hit.get("genres") or [], "exact": exact is not None}
            if best is None or (item["exact"] and not best["exact"]):
                best = item
        return best

    def _learn_cand(self, chat_id: int, action: str, c: Any) -> None:
        if not self.profiles:
            return
        tmdb_id = c.ext_id if c.kind == "movie" else c.tmdb_id
        self.profiles.learn(chat_id, action, c.kind, c.title, tmdb=tmdb_id,
                            tvdb=c.ext_id if c.kind == "series" else None, year=c.year, genres=c.genres)

    def offer_mode(self, offer_id: str, mode: str) -> Offer | None:
        offer = self.offers.get(offer_id)
        if offer is not None:
            offer.mode = mode
        return offer

    def offer_feedback(self, offer_id: str, idx: int, action: str, user_id: int) -> tuple[Offer, str, str | None] | None:
        """👍 (l), 👎 (d) or 👁 seen (s) on one title of a suggestion list. Returns the offer, a short
        answer and for 👁 the id of a rating question ("wie fandest du's?")."""
        offer = self.offers.get(offer_id)
        if offer is None or not 0 <= idx < len(offer.cands) or action not in ("l", "d", "s"):
            return None
        c = offer.cands[idx]
        if idx not in offer.hidden:
            offer.hidden.append(idx)
        if action == "s":
            return offer, f"👁 {c.title}", self.ask_rating(user_id, c)
        self._learn_cand(user_id, "liked" if action == "l" else "disliked", c)
        return offer, (f"👍 {c.title}" if action == "l" else f"👎 {c.title} – schlage ich nicht mehr vor"), None

    def ask_rating(self, user_id: int, c: Any) -> str:
        """Remember a question "wie fandest du X?" (buttons ⭐1–5). Returns its id."""
        ask_id = uuid.uuid4().hex[:10]
        self.rating_asks[ask_id] = c
        while len(self.rating_asks) > 300:
            self.rating_asks.pop(next(iter(self.rating_asks)))
        self._learn_cand(user_id, "asked", c)
        return ask_id

    def rate(self, ask_id: str, stars: int, user_id: int) -> str | None:
        c = self.rating_asks.pop(ask_id, None)
        if c is None:
            return None
        if stars >= 4:
            self._learn_cand(user_id, "liked", c)
            return f"👍 {c.title}: {'⭐' * stars} – gemerkt, davon suche ich mehr."
        if 1 <= stars <= 2:
            self._learn_cand(user_id, "disliked", c)
            return f"👎 {c.title}: {'⭐' * stars} – gemerkt, sowas schlage ich seltener vor."
        return f"👌 {c.title}: gemerkt." if stars == 3 else "👌 Alles klar."

    async def _t_title_feedback(self, chat_id: int, title: str, opinion: str, kind: str | None = None) -> Final:
        hit = await self._resolve_title(kind, title)
        if hit is None:
            return Final(f"🤷 „{title}“ habe ich nicht gefunden – Film oder Serie, und wie heißt er genau?")
        action = {"liked": "liked", "disliked": "disliked", "seen": "asked"}.get(opinion, "asked")
        if self.profiles:
            self.profiles.learn(chat_id, action, hit["kind"], hit["title"], tmdb=hit["tmdb"], tvdb=hit["tvdb"],
                                year=hit["year"], genres=hit["genres"])
        return Final({"liked": f"👍 Gemerkt: {hit['title']} gefällt dir – davon suche ich mehr.",
                      "disliked": f"👎 Gemerkt: {hit['title']} ist nicht deins.",
                      }.get(opinion, f"👁 Gemerkt: {hit['title']} kennst du schon – schlage ich nicht vor."))

    def _learn(self, chat_id: int, action: str, kind: str, item: dict, title: str | None = None) -> None:
        if not self.profiles:
            return
        name = title or (f"{item.get('title')} ({item.get('year')})" if kind == "movie" and item.get("year")
                         else item.get("title", "?"))
        self.profiles.learn(chat_id, action, kind, name, tmdb=item.get("tmdbId"),
                            tvdb=item.get("tvdbId") if kind == "series" else None,
                            year=item.get("year") or None, genres=item.get("genres") or [])

    def reject_offer(self, offer_id: str, chat_id: int) -> str | None:
        """'Nichts davon' under suggestions: never show them again, their genres count a bit less."""
        offer = self.offers.pop(offer_id, None)
        if offer is None:
            return None
        for i, c in enumerate(offer.cands):
            if i not in offer.hidden:
                self._learn_cand(chat_id, "not_interested", c)
        return "👍 Verstanden, die schlage ich nicht mehr vor. Frag einfach nochmal nach neuen Ideen."

    async def _t_remember(self, chat_id: int, like: list[str] | None = None, avoid: list[str] | None = None,
                          from_year: int | None = None, min_rating: float | None = None, note: str | None = None,
                          forget: list[str] | None = None) -> Final:
        if not self.profiles:
            return Final("Profile sind nicht eingerichtet.")
        return Final(self.profiles.remember(chat_id, like=like, avoid=avoid, from_year=from_year,
                                            min_rating=min_rating, note=note, forget=forget))

    async def _t_my_profile(self, chat_id: int) -> Final:
        self._outbox.setdefault(chat_id, []).append(View(chat_id, "profile"))
        return Final("")

    async def import_history(self) -> int:
        """First start with profiles: learn from the old request list (genres from the library)."""
        if not self.profiles or self.profiles.has_events() or not self.store.state.requests:
            return 0
        lib: dict[tuple[str, int], dict] = {}
        for kind, svc, key in (("movie", self.radarr, "tmdbId"), ("series", self.sonarr, "tvdbId")):
            if svc:
                try:
                    lib.update({(kind, i[key]): i for i in await svc.library() if i.get(key)})
                except ArrError as e:
                    log.warning("Profil-Import: %s", e)
        for r in self.store.state.requests:
            item = lib.get((r["kind"], r.get("tmdb") if r["kind"] == "movie" else r.get("tvdb")), {})
            self.profiles.learn(r["chat_id"], "requested", r["kind"], r["title"], tmdb=r.get("tmdb"),
                                tvdb=r.get("tvdb"), year=item.get("year"), genres=item.get("genres") or [])
        return len(self.store.state.requests)

    async def _t_top_titles(
        self, chat_id: int, kind: str, year: int | None = None, month: int | None = None, count: int = 5,
        sort: str = "popular", download: bool = False, studio: str | None = None,
    ) -> Final:
        if not self.tmdb:
            return Final(NO_TMDB)
        self._svc(kind)
        count = max(1, min(int(count or 5), MAX_TOP))
        start: date | None = None
        end: date | None = None
        period = ""
        if year and month:
            year, month = int(year), int(month)
            start = date(year, month, 1)
            end = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
            period = f"{MONTHS[month - 1]} {year}"
        elif year:
            start, end, period = date(int(year), 1, 1), date(int(year), 12, 31), str(year)
        companies = None
        if studio:
            companies = await self.tmdb.companies(studio)
            if not companies:
                return Final(f"🤷 Ein Studio „{studio}“ kenne ich bei TMDB nicht.")
        recs = await self.tmdb.top(kind, start, end, sort, companies)
        owned = await self._library_ids([kind])

        cands: list[tuple[Candidate, bool]] = []
        for r in recs:
            if len(cands) >= count:
                break
            if kind == "movie":
                c, have = Candidate("movie", r.tmdb_id, r.title, r.year), ("movie", r.tmdb_id) in owned["library"]
            else:
                try:
                    tvdb = await self.tmdb.tvdb_from_tv(r.tmdb_id)
                except TmdbError:
                    continue
                if not tvdb:
                    continue
                c, have = Candidate("series", tvdb, r.title, r.year), tvdb in owned["tvdb"]
            c.rating = r.rating
            self._remember(chat_id, c)
            cands.append((c, have))
        if not cands:
            return Final(f"🤷 Für {period or studio or 'diese Suche'} habe ich bei TMDB keine Daten gefunden.")

        what = "Filme" if kind == "movie" else "Serien"
        how = {"rating": "bestbewertet", "revenue": "nach Einspielergebnis"}.get(sort, "meistgesehen")
        label = f"{studio}-{what}" if studio else what
        head = f"🏆 Top {len(cands)} {label}{' ' + period if period else ''} ({how}):"
        if not download:
            lines = [f"{i}. {c.title}" + (f" ({c.year})" if c.year else "")
                     + (f" ⭐ {c.rating:.1f}".replace(".", ",") if c.rating else "") + (" ✅ schon da" if have else "")
                     for i, (c, have) in enumerate(cands, 1)]
            new = [c for c, have in cands if not have]
            if not new:
                return Final("\n".join([head, *lines, "", "👍 Die habt ihr schon alle."]))
            hint = "Tippe einen Titel an, um ihn hinzuzufügen."
            if len(new) < len(cands):
                hint += " Was schon da ist, hat keinen Button."
            self._offer(chat_id, new, "\n".join([head, *lines, "", hint]), bulk=True)
            return Final("")

        lines = [head]
        for i, (c, have) in enumerate(cands, 1):
            name = c.title + (f" ({c.year})" if c.year else "")
            if have:
                lines.append(f"{i}. 👍 {name} – schon da")
                continue
            try:
                await self._add(Flow(uuid.uuid4().hex[:10], chat_id, c, monitor="all"))
                lines.append(f"{i}. ✅ {name} – hinzugefügt")
            except Held:
                lines.append(f"{i}. ⏳ {name} – wartet auf Freigabe")
            except ArrError as e:
                lines.append(f"{i}. ❌ {name} – {e}")
        lines.append("\nQualität: Standardprofil. Ich melde mich, wenn Downloads starten.")
        return Final("\n".join(lines))

    async def _library_ids(self, kinds: list[str]) -> dict:
        # tmdb: library + already requested (to skip in suggestions); library: only what Radarr has
        # server: on disk (movie with file, series with at least one episode file)
        owned: dict[str, Any] = {"tmdb": set(), "tvdb": set(), "library": set(), "server": set(), "recent": []}
        recent = []
        if "movie" in kinds and self.radarr:
            for m in await self.radarr.library():
                owned["tmdb"].add(("movie", m.get("tmdbId")))
                owned["library"].add(("movie", m.get("tmdbId")))
                if m.get("hasFile"):
                    owned["server"].add(("movie", m.get("tmdbId")))
                recent.append((m.get("added") or "", "movie", m.get("tmdbId"), m.get("title")))
        if "series" in kinds and self.sonarr:
            for x in await self.sonarr.library():
                owned["tvdb"].add(x.get("tvdbId"))
                if x.get("tmdbId"):
                    owned["tmdb"].add(("series", x["tmdbId"]))
                    if (x.get("statistics") or {}).get("episodeFileCount"):
                        owned["server"].add(("series", x["tmdbId"]))
                    recent.append((x.get("added") or "", "series", x["tmdbId"], x.get("title")))
        for r in self.store.state.requests:
            if r.get("tmdb"):
                owned["tmdb"].add((r["kind"], r["tmdb"]))
        owned["recent"] = [(k, i, t) for _, k, i, t in sorted(recent, key=lambda x: x[0], reverse=True) if i]
        return owned
