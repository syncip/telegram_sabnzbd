"""🛠 Admin overview, user card, version/model info (offline)."""

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from arrbot import activity, admin, guard, toolgroups
from arrbot.agent import PRICES
from arrbot.llm import ToolCall
from arrbot.tools import ToolRunner, View
from arrbot.users import BLOCKED, PENDING, UserStore

from .test_features import make

NOW = datetime.now(timezone.utc)  # usage counters roll by the real date
TZ = ZoneInfo("Europe/Berlin")


class Prov:
    reasoning, reasoning_ok = "low", True


def test_model_text_prices_and_shutdown_warning():
    m = admin.model_of(Prov(), "gpt-6-luna")
    assert m.text(PRICES) == "gpt-6-luna (Denken: low) · 0,10 $ rein / 0,50 $ raus pro 1 Mio. Tokens"
    old = admin.Model("gpt-4o-mini", "low", reasoning_ok=False)
    assert "(ohne Denk-Stufe)" in old.text(PRICES) and "wird am 14.01.2027 abgeschaltet" in old.text(PRICES)
    txt = admin.info_text("🏷 Version 1.9.0", m, admin.Model("gpt-6.1-terra", "low"), PRICES, "openai", True, "Neu: x")
    assert "🧠 KI (openai): gpt-6-luna" in txt and "🛠 Werkzeug-KI: gpt-6.1-terra" in txt and txt.endswith("Neu: x")
    user = admin.info_text("🏷 Version 1.9.0", m, admin.Model("gpt-6.1-terra"), PRICES, "openai", False)
    assert "Werkzeug" not in user and "$" not in user  # users: just version and model


def test_dashboard(tmp_path):
    users = UserStore(str(tmp_path), admin_id=1, seed_users={5, 6})
    users.request(7, "Neu")
    users.set_role(6, BLOCKED)
    users.count_request(5)
    users.add_usage(5, 1000, 0.004)
    users.set_limit("gesamt", "kosten", 1)
    users.users[5].usage.day = NOW.strftime("%Y-%m-%d")
    e = [activity.Entry(1, 5, NOW - timedelta(hours=2), "x", ["add_movie"], activity.OK, "", 10),
         activity.Entry(2, 5, NOW - timedelta(hours=3), "y", ["add_movie"], activity.ERROR, "kaputt", 10)]
    ev = [guard.Event(NOW, 6, guard.BLOCK, "Spam")]
    text = admin.dashboard(version_line="🏷 Version 1.9.0", models=["🧠 KI: gpt-6-luna"], users=users,
                           pending_approvals=2, entries=e, events=ev, monitor_status="🛡 alles läuft", recipes=1,
                           now=NOW)
    assert "2 freigeschaltet · heute aktiv 1 · ⏳ 1 warten · 🚫 1 gesperrt" in text
    assert "0,40 ct" in text and "0 % vom Budget (1,00 $)" in text
    assert "2 Fragen, 50 % geklappt · ⚠️ 1 Probleme" in text
    assert "1 Zugangsanfrage · 2 Wünsche zur Freigabe · 1 Sperren, 0 Warnungen" in text


def test_user_card(tmp_path):
    users = UserStore(str(tmp_path), admin_id=1, seed_users={5})
    users.users[5].name = "Anna"
    users.set_limit(5, "tag", 5)
    users.set_limit("standard", "monat", 50)
    users.apply_quality("standard", "1080")
    reqs = [{"kind": "movie", "title": f"Film {i}", "chat_id": 5} for i in range(7)] + \
           [{"kind": "series", "title": "Dark", "chat_id": 5}, {"kind": "movie", "title": "Fremd", "chat_id": 9}]
    ev = [guard.Event(NOW, 5, guard.WARN, "10 Titel in einer Stunde")]
    text = admin.user_card(users.users[5], users, reqs, [], ev, ["🎬 Plex: nicht verbunden"], TZ, now=NOW)
    assert text.startswith("👤 <b>Anna</b> <code>5</code> · 👤 Nutzer")
    assert "5 Anfragen pro Tag" in text and "(Standard)" in text and "Full HD (1080p) (Standard)" in text
    assert "Gewünscht: 8 (7 Filme, 1 Serien)" in text and "Zuletzt: Dark, Film 6" in text and "Fremd" not in text
    assert "Warnung: 10 Titel" in text and text.endswith("🎬 Plex: nicht verbunden")
    assert "unbegrenzt" in admin.user_card(users.users[1], users, [], [], [], [], TZ, now=NOW)


def test_chat_tools_and_groups(tmp_path):
    fr, fs, radarr, sonarr, store = make(tmp_path)
    tools = ToolRunner(radarr, sonarr, store, "released")
    tools.users = UserStore(str(tmp_path), admin_id=1, seed_users={5})
    tools.users.users[5].name = "Anna"
    tools.is_admin = tools.users.is_admin

    async def info(uid):
        return f"🏷 Version für {uid}"

    tools.bot_info = info
    assert asyncio.run(tools.run(ToolCall("1", "bot_info", {}), 5)).final == "🏷 Version für 5"  # everyone
    assert "bot_info" in {s.name for s in tools.specs_for(5)}
    assert asyncio.run(tools.run(ToolCall("2", "admin_overview", {}), 5)).is_error
    asyncio.run(tools.run(ToolCall("3", "admin_user_info", {"who": "anna"}), 1))
    assert tools.take_outbox(1) == [View(1, "usercard", arg=5)]
    assert "extras" in toolgroups.groups_for("Welche Version hast du?")
    assert "extras" in toolgroups.groups_for("welches Modell nutzt du")
    assert "admin" in toolgroups.groups_for("Zeig mir die Übersicht")


def test_pending_users_waiting(tmp_path):
    users = UserStore(str(tmp_path), admin_id=1)
    users.request(7, "Neu")
    assert users.role(7) == PENDING
