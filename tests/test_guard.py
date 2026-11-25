"""🚨 Spam check, overuse warnings and the monthly report (offline)."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from arrbot import activity, guard, monthly
from arrbot.recipes import Recipe
from arrbot.users import BLOCKED, USER, UserStore

T0 = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
TZ = ZoneInfo("Europe/Berlin")


def test_burst_and_repeat_block(tmp_path):
    g = guard.Guard(str(tmp_path))
    for i in range(guard.BURST_MSGS):
        assert g.message(5, f"Frage {i}", T0 + timedelta(seconds=i)) is None
    v = g.message(5, "noch eine", T0 + timedelta(seconds=9))
    assert v.kind == guard.BLOCK and "Nachrichten in 60 Sekunden" in v.reason
    assert g.message(6, "a", T0) is None and g.message(6, "b", T0 + timedelta(minutes=2)) is None  # others unaffected

    g2 = guard.Guard(str(tmp_path))
    for i in range(guard.REPEAT_MSGS - 1):
        assert g2.message(7, "Lad Dune!!", T0 + timedelta(minutes=i)) is None
    v = g2.message(7, "lad dune", T0 + timedelta(minutes=3))
    assert v.kind == guard.BLOCK and "dieselbe Nachricht" in v.reason


def test_adds_warn_once_then_block(tmp_path):
    g = guard.Guard(str(tmp_path))
    verdicts = [g.added(5, T0 + timedelta(minutes=i)) for i in range(guard.ADDS_BLOCK + 1)]
    warns = [v for v in verdicts if v and v.kind == guard.WARN]
    assert len(warns) == 1 and verdicts[guard.ADDS_WARN - 1] is warns[0]
    assert verdicts[-1].kind == guard.BLOCK
    assert guard.Guard(str(tmp_path), enabled=False).added(5) is None


def test_limit_warnings_once_per_period(tmp_path):
    users = UserStore(str(tmp_path), admin_id=1, seed_users={5})
    users.set_limit(5, "tag", 10)
    g = guard.Guard(str(tmp_path))
    u = users.users[5]
    for _ in range(7):
        users.count_request(5)
    assert g.limits(5, u.usage, users.limits_for(5)) is None
    users.count_request(5)
    v = g.limits(5, u.usage, users.limits_for(5))
    assert v.kind == guard.WARN and "8 von 10" in v.reason
    users.count_request(5)
    assert g.limits(5, u.usage, users.limits_for(5)) is None  # 80 % told already
    users.count_request(5)
    assert "Limit erreicht" in g.limits(5, u.usage, users.limits_for(5)).reason


def test_unusual_compared_with_the_others(tmp_path):
    g = guard.Guard(str(tmp_path))
    found = g.unusual({5: 40, 6: 4, 7: 6, 8: 0}, T0)
    assert [uid for uid, _ in found] == [5] and "üblich sind etwa 6" in found[0][1].reason
    assert g.unusual({5: 41, 6: 4, 7: 6}, T0) == []  # once a day
    assert g.unusual({5: 40}, T0) == []  # nobody to compare with


def test_unblock_resets_the_counters(tmp_path):
    users = UserStore(str(tmp_path), admin_id=1, seed_users={5})
    g = guard.Guard(str(tmp_path))
    users.on_role = lambda uid, old, new: g.forget(uid)
    for i in range(guard.BURST_MSGS + 1):
        g.message(5, str(i))
    users.set_role(5, BLOCKED)
    users.set_role(5, USER)
    assert g.message(5, "hallo") is None


def test_monthly_report(tmp_path):
    users = UserStore(str(tmp_path), admin_id=1, seed_users={5, 6})
    users.users[5].name, users.users[6].name = "Anna", "Bob"
    users.users[6].since = datetime(2026, 10, 3, tzinfo=timezone.utc).isoformat()
    users.users[6].role = BLOCKED
    users.total.month = "2026-11"
    users.total.months = {"2026-10": {"requests": 120, "tokens": 300000, "cost": 0.12},
                          "2026-09": {"requests": 100, "tokens": 250000, "cost": 0.10}}
    users.users[5].usage.month = "2026-11"
    users.users[5].usage.months = {"2026-10": {"requests": 90, "tokens": 200000, "cost": 0.08}}
    oct_ = datetime(2026, 10, 10, 12, tzinfo=timezone.utc).isoformat()
    requests = [{"kind": "movie", "title": "Dune", "chat_id": 5, "at": oct_},
                {"kind": "movie", "title": "Dune", "chat_id": 6, "at": oct_},
                {"kind": "series", "title": "Dark", "chat_id": 5, "at": oct_},
                {"kind": "movie", "title": "Später", "chat_id": 5, "at": T0.isoformat()}]
    at = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    entries = [activity.Entry(i, 5, at, "x", ["add_movie"], o, d, 100) for i, (o, d) in enumerate(
        [(activity.OK, ""), (activity.OK, ""), (activity.PROBLEM, "Radarr nicht erreichbar"), (activity.TALK, "")])]
    events = [guard.Event(at, 6, guard.BLOCK, "9 Nachrichten in 60 Sekunden"),
              guard.Event(at, 5, guard.WARN, "10 Titel in einer Stunde hinzugefügt")]
    built = [Recipe("studio_top", "Top-Filme eines Studios", "x", line="{title}", created=oct_, cost=0.017, uses=4)]
    text = monthly.render(monthly.previous_month(date(2026, 11, 1)), users, requests, entries, events, built, TZ)
    assert text.startswith("🗓 <b>Monatsbericht Oktober 2026</b>")
    assert "0,12 $ (+20 % zum Vormonat)" in text and "120 Anfragen (+20 % zum Vormonat) · 300.000 Tokens" in text
    assert "Anna: 90 Anfragen · 0,08 $" in text and "neu: Bob" in text and "gesperrt: Bob" in text
    assert "3 Titel (2 Filme, 1 Serien)" in text and "Mehrfach gewünscht: Dune (2×)" in text
    assert "66 % von 3 Fragen geklappt" in text and "1× Radarr nicht erreichbar" in text
    assert "1 vorsorgliche Sperren, 1 Warnungen" in text and "🚫 10.10. Bob: 9 Nachrichten" in text
    assert "🛠 <b>Neue Werkzeuge</b>: 1 (0,02 $): Top-Filme eines Studios" in text
