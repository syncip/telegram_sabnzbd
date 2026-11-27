# arrbot – map for editing

Telegram bot (German UI) for Radarr, Sonarr, SABnzbd, TMDB, Plex. Python 3, python-telegram-bot, Docker on Unraid,
image `ghcr.io/syncip/telegram_sabnzbd`. Code and comments in English, everything the user sees in German.

**Saving tokens:** do not read whole files. Find the place with Grep (names below, or `# ---- ` section markers),
then Read only that range. Every module's first docstring line says what it does.

## Checks (run before saying "done")

```
.venv/Scripts/python -m pyflakes arrbot tests && .venv/Scripts/python -m pytest -q
grep -nE '(KEY|TOKEN|ID): "[^"]+' docker-compose.yml   # only PUID/PGID may match
```

## Rules

- Commit and push only when the user says so ("Ja, pushen"). Never `--no-verify`. No secrets (`.env`, compose keys).
- Each release: bump `__version__` and add a `CHANGES` entry in `arrbot/version.py`, update `README.md`.
- Bigger edits: a Python patch script in the scratchpad with `assert old in s; s.replace(old, new, 1)`.
- Texts the bot sends from flows/buttons are plain text (no HTML) unless sent with `ParseMode.HTML`.

## Where things live

| Area | Files |
|---|---|
| AI loop, system prompt (tool hints per sentence type) | `agent.py` (`SYSTEM_PROMPT`), `llm.py` (providers) |
| Which tools the AI gets per message (keywords) | `toolgroups.py` (`GROUPS`; all `admin_*` only via the `admin` group) |
| Tool descriptions/schemas the AI sees | `toolspecs.py` (`build_specs`, `build_admin_specs`) |
| ToolRunner core: rights (`may`, `ADMIN_ONLY`, `TOOL_PERMS`, `owner_ok`), `run`, flows (`_advance`, `_choose`), add/search/remove/pause | `tools.py` |
| Tool code by theme (mixins of ToolRunner, `self` = ToolRunner) | `tools_media.py`, `tools_admin.py`, `tools_discover.py`, `tools_german.py`, `tools_books.py` |
| Flow/Offer/Step/View/Final/Candidate, constants, small helpers | `tooltypes.py` (re-exported by `tools.py`) |
| Telegram handlers, jobs, buttons | `bot.py` – one `build_app()`; handlers share its locals, so it stays one function |
| Users, roles, limits (AI + titles), rights | `users.py`; limit advice `limitadvice.py` |
| Downloads tracking + notices | `tracker.py`, `state.py`, `notify.py`, `downloads.py`, `diagnose.py`, `langpick.py` |
| Library / cleanup / duplicates / watched questions | `media.py`, `mediamgr.py`, `cleanup.py`, `dupes.py`, `watchask.py` |
| German audio | `german.py`, `manage.py`, `listen.py`, `watch.py` |
| Suggestions & taste | `recommend.py`, `profiles.py`, `together.py`, `wishlist.py`, `tmdb.py` |
| Books (Chaptarr, file into chat, pending in data/books.json, format per chat + Calibre convert, suggestions) | `chaptarr.py`, `books.py`, `tools_books.py` |
| Book lists (Open Library: new releases, trending) | `booklists.py` |
| Chaptarr like Radarr/Sonarr | `health.run_checks(chaptarr=)`, `arr_services` in `bot.py`, `downloads.collect(chaptarr=)`, `extras.disks` |

## bot.py sections (Grep the marker `# ---- <name>`)

search done (why nothing loaded, language offer) · chat log · mute · conversation (`respond`, `busy_with`,
`deliver_outbox`) · button flows (`show_flow`, `on_flow_button`, `on_notice_button`, `on_lang_pick`) · downloads
overview · statistics and API check · admin overview and user card · approvals · cleanup rules · activity · media
check · calendar and disk space · Plex · daily / hourly jobs (`*_job`, scheduled at the end of `build_app`) · user
profile · access requests and user management (`/limit`) · commands · misc (handler registration).

Button callback prefixes: `f:` flow, `o:` offer, `rt:` rating, `n:` no-release notice (`n:lg:` language),
`d:` downloads, `u:` users, `s:` stats, `p:` profile, `x:` Plex, `m:` media, `a:` activity, `c:` cleanup,
`r:` approvals, `g:` admin, `h:` health, `nt:` notify settings.

## Adding a tool (checklist)

1. Spec in `toolspecs.py` (admin tools: name starts with `admin_`, in `build_admin_specs`).
2. `async def _t_<name>(self, chat_id, ...) -> Final` in the fitting mixin (return `Final("")` and push a
   `Flow`/`View` into `self._outbox` for button answers).
3. Users may use it? Otherwise it is admin-only by the `admin_` prefix or `ADMIN_ONLY`; per-user rights: `TOOL_PERMS`.
4. Not `admin_*`: add it to a group in `toolgroups.py` (unknown tools are always sent – costs tokens).
5. A line in `SYSTEM_PROMPT` if the AI must pick it for certain sentences.
6. Button flow: new `stage`, a branch in `ToolRunner._choose`, a rule in `owner_ok`.
7. Test in `tests/` (fakes for Radarr/Sonarr/Plex; see `tests/test_watchask.py`, `tests/test_mediamgr.py`).
