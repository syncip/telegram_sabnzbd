"""Admin statistics: users, requests, tokens and costs – rendered in code (no AI tokens)."""

from __future__ import annotations

import html
from datetime import datetime, tzinfo

from .downloads import bar
from .state import Store, utcnow
from .users import ADMIN, BLOCKED, PENDING, USER, UserStore, limit_text

MAX_USERS = 15


def _n(x: float) -> str:
    return f"{int(x):,}".replace(",", ".")


def _usd(x: float) -> str:
    return (f"{x:.4f} $" if x < 0.1 else f"{x:.2f} $").replace(".", ",")


def _ago(iso: str, tz: tzinfo, now: datetime) -> str:
    if not iso:
        return "nie"
    t = datetime.fromisoformat(iso).astimezone(tz)
    days = (now.astimezone(tz).date() - t.date()).days
    if days == 0:
        return f"heute {t:%H:%M}"
    if days == 1:
        return f"gestern {t:%H:%M}"
    return f"vor {days} Tagen" if days < 30 else f"{t:%d.%m.%Y}"


def render(users: UserStore, store: Store, model: str, tz: tzinfo, price_known: bool = True,
           now: datetime | None = None) -> str:
    now = now or utcnow()
    e = lambda s: html.escape(str(s), quote=False)  # noqa: E731
    all_users = users.listing()
    for u in all_users:
        u.usage.roll()
    users.total.roll()
    tot = users.total
    month = now.astimezone(tz).strftime("%Y-%m")

    roles = {r: sum(1 for u in all_users if u.role == r) for r in (ADMIN, USER, PENDING, BLOCKED)}
    active_month = sum(1 for u in all_users if u.usage.month_requests)
    active_today = sum(1 for u in all_users if u.usage.day_requests)
    out = [
        "<b>📊 Statistik</b>",
        f"\n<b>👥 Nutzer: {roles[ADMIN] + roles[USER]}</b> ({roles[ADMIN]} Admin, {roles[USER]} Nutzer"
        + (f", {roles[PENDING]} wartend" if roles[PENDING] else "")
        + (f", {roles[BLOCKED]} gesperrt" if roles[BLOCKED] else "") + ")",
        f"Aktiv heute {active_today} · diesen Monat {active_month}",
    ]

    cost_note = "" if price_known else " <i>(kein Preis für dieses Modell hinterlegt)</i>"
    per_req = tot.total_tokens / tot.total_requests if tot.total_requests else 0
    per_req_cost = tot.total_cost / tot.total_requests if tot.total_requests else 0
    out += [
        f"\n<b>🤖 KI ({e(model)})</b>{cost_note}",
        f"Heute: {tot.day_requests} Anfragen",
        f"Monat: {tot.month_requests} Anfragen · {tot.month_calls} KI-Aufrufe\n"
        f"   {_n(tot.month_tokens)} Tokens · {_usd(tot.month_cost)}",
        f"Gesamt: {tot.total_requests} Anfragen · {tot.total_calls} KI-Aufrufe\n"
        f"   {_n(tot.total_tokens)} Tokens · {_usd(tot.total_cost)}",
    ]
    if tot.total_requests:
        out.append(f"Ø pro Anfrage: {_n(per_req)} Tokens · {_usd(per_req_cost)}")
    budget = users.total_limits.get("kosten")
    if budget:
        pct = min(100.0, 100 * tot.month_cost / budget)
        out.append(f"Budget: {_usd(tot.month_cost)} von {_usd(budget)}\n<code>{bar(pct)}</code> {pct:.0f} %")
    if tot.months:
        out.append("\n<b>📈 Letzte Monate</b>")
        for m in sorted(tot.months, reverse=True)[:6]:
            d = tot.months[m]
            out.append(f"{m}: {d.get('requests', 0)} Anfragen · {_n(d.get('tokens', 0))} Tokens · {_usd(d.get('cost', 0))}")

    reqs = store.state.requests
    month_reqs = [r for r in reqs if r.get("at", "")[:7] == month]
    finished = [f for f in store.state.finished if f.get("at", "")[:7] == month]
    out += [
        "\n<b>🎬 Titel</b>",
        f"Diesen Monat angefragt: {len(month_reqs)} ({sum(r['kind'] == 'movie' for r in month_reqs)} Filme, "
        f"{sum(r['kind'] == 'series' for r in month_reqs)} Serien) · fertig: {len(finished)}",
        f"Gerade beobachtet: {len(store.state.tracked)} · pausiert: {len(store.state.paused)}",
    ]

    out.append("\n<b>👤 Pro Nutzer</b>")
    ranked = sorted(all_users, key=lambda u: (u.usage.month_tokens, u.usage.total_tokens), reverse=True)
    for u in ranked[:MAX_USERS]:
        us = u.usage
        titles = sum(1 for r in reqs if r["chat_id"] == u.id)
        titles_month = sum(1 for r in month_reqs if r["chat_id"] == u.id)
        share = f" · {100 * us.month_tokens / tot.month_tokens:.0f} % der Tokens" if tot.month_tokens else ""
        lim = users.limits_for(u.id)
        lim_txt = ", ".join(limit_text(k, v) for k, v in lim.items()) or "keine Limits"
        icon = {ADMIN: "👑", USER: "👤", PENDING: "⏳", BLOCKED: "🚫"}.get(u.role, "")
        out.append(
            f"{icon} <b>{e(u.name)}</b>{share}\n"
            f"   Anfragen: heute {us.day_requests} · Monat {us.month_requests} · gesamt {us.total_requests}\n"
            f"   Monat {_n(us.month_tokens)} Tokens · {_usd(us.month_cost)} (gesamt {_usd(us.total_cost)})\n"
            f"   Titel: {titles_month} diesen Monat, {titles} gesamt · zuletzt {_ago(us.last_active, tz, now)}\n"
            f"   <i>{e(lim_txt)}</i>"
        )
    if len(ranked) > MAX_USERS:
        out.append(f"<i>… und {len(ranked) - MAX_USERS} weitere</i>")
    out.append(f"\n<i>Stand {now.astimezone(tz):%d.%m. %H:%M} Uhr</i>")
    return "\n".join(out)[:4096]
