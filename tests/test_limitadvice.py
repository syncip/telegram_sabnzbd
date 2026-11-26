"""Total limits per day, warnings at 80 % / 100 %, and limit advice from the last days."""

import asyncio
from datetime import date, timedelta

from arrbot import limitadvice
from arrbot.llm import ToolCall
from arrbot.state import Store
from arrbot.tools import Flow, ToolRunner
from arrbot.users import USER, UserStore

ADMIN, SARAH = 1, 2


def test_daily_total_limit_and_warnings(tmp_path):
    users = UserStore(str(tmp_path), ADMIN)
    users.set_role(SARAH, USER, "Sarah")
    assert "2.000 Tokens pro Tag" in users.apply_limit("gesamt", "tokens_tag", "2000")
    users.add_usage(SARAH, 1500, 0.001)
    assert users.blocked_by_limit(SARAH) is None and users.total_warnings() == []
    users.add_usage(SARAH, 200, 0.001)
    w = users.total_warnings()
    assert len(w) == 1 and w[0].startswith("⚠️ Gesamtbudget heute: 85 %")
    assert users.total_warnings() == []  # only once
    users.add_usage(ADMIN, 400, 0.001)
    assert users.total_warnings()[0].startswith("⛔ Gesamtbudget heute: 105 %")
    assert "für heute aufgebraucht" in users.blocked_by_limit(SARAH)
    assert "für heute aufgebraucht" in users.blocked_by_limit(ADMIN)  # a hard budget: for everyone


def fill(users, uid, days):
    """days: list of (tokens, cost, requests), oldest first, the last one is today."""
    u = users.users[uid].usage if uid else users.total
    u.roll()
    today = date.fromisoformat(u.day)
    for i, (t, c, r) in enumerate(days[:-1]):
        d = (today - timedelta(days=len(days) - 1 - i)).isoformat()
        u.days[d] = {"tokens": t, "cost": c, "requests": r}
    u.day_tokens, u.day_cost, u.day_requests = days[-1]


def test_advice_with_margin_and_apply(tmp_path):
    users = UserStore(str(tmp_path), ADMIN)
    users.set_role(SARAH, USER, "Sarah")
    week = [(40_000, 0.01, 20)] * 13 + [(90_000, 0.03, 40)]  # 14 days, one busy day
    fill(users, 0, week)
    fill(users, SARAH, [(20_000, 0.005, 10)] * 13 + [(50_000, 0.01, 25)])
    fill(users, ADMIN, week)
    a = limitadvice.advise(users)
    assert a.days == 14 and a.active_users == 2 and a.peak_tokens == 90_000
    assert a.margin == 1.6  # 14 days of data, two users
    assert a.total["tokens_tag"] == 200_000  # 90k peak x 1.6 = 144k -> 200k
    assert a.total["tokens"] >= a.total["tokens_tag"] * 10
    assert a.standard["tokens_tag"] == 100_000 and a.standard["tag"] == 50  # Sarah's peak x 1.6, rounded up
    text = limitadvice.render(a, users)
    assert "14 Tage, 2 aktive Nutzer" in text and "200.000 Tokens pro Tag (jetzt: keins)" in text

    tools = ToolRunner(None, None, Store(str(tmp_path)), "released")
    tools.users, tools.is_admin = users, users.is_admin
    asyncio.run(tools.run(ToolCall("1", "admin_limit_advice", {}), ADMIN))
    flow = next(x for x in tools.take_outbox(ADMIN) if isinstance(x, Flow))
    assert [v for _, v in flow.step.buttons] == ["all", "total", "standard", "no"]
    assert not tools.owner_ok("flow", flow.id, SARAH, ADMIN)  # only admins take it over
    flow = asyncio.run(tools.choose(flow.id, "total"))
    assert flow.step.done and users.total_limits["tokens_tag"] == 200_000 and "tokens_tag" not in users.defaults


def test_little_data_bigger_margin(tmp_path):
    users = UserStore(str(tmp_path), ADMIN)
    fill(users, 0, [(10_000, 0.0, 5)] * 3)
    fill(users, ADMIN, [(10_000, 0.0, 5)] * 3)
    a = limitadvice.advise(users)
    assert a.days == 3 and a.margin == 3.0 and a.total["tokens_tag"] == 50_000 and "kosten_tag" not in a.total
    assert "wenig Daten" in limitadvice.render(a, users)
    empty = limitadvice.advise(UserStore(str(tmp_path / "x"), ADMIN))
    assert "zu wenig Verbrauch" in limitadvice.render(empty, users)


def test_nice_rounding():
    assert [limitadvice.nice(x) for x in (144_000, 1, 0.021, 2600)] == [200_000, 1, 0.025, 5000]
