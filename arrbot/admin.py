"""🛠 Admin overview (/admin), user card and "which version/model is running" – all rendered in code."""

from __future__ import annotations

import html
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from . import activity, guard
from .users import ADMIN, BLOCKED, PENDING, PERMS, USER, limit_text, quality_label

# Models OpenAI shuts down (date shown as a warning wherever the model is named).
SHUTDOWN = {"gpt-4o-mini": "14.01.2027", "gpt-4o": "14.01.2027", "gpt-4.1": "14.01.2027",
            "gpt-4.1-mini": "14.01.2027", "gpt-4.1-nano": "23.10.2026"}


@dataclass
class Model:
    name: str
    reasoning: str = ""  # configured thinking level
    reasoning_ok: bool = True  # False: the model ignored/refused it, the bot sends it no more

    def text(self, prices: dict) -> str:
        out = self.name
        if self.reasoning:
            out += f" (Denken: {self.reasoning})" if self.reasoning_ok else " (ohne Denk-Stufe)"
        p = prices.get(self.name)
        if p:
            rein, raus = (f"{x:.2f}".replace(".", ",") for x in (p[0], p[2]))
            out += f" · {rein} $ rein / {raus} $ raus pro 1 Mio. Tokens"
        if self.name in SHUTDOWN:
            out += f" · ⚠️ wird am {SHUTDOWN[self.name]} abgeschaltet"
        return out


def model_of(provider: Any, name: str) -> Model:
    return Model(name, getattr(provider, "reasoning", "") or "", getattr(provider, "reasoning_ok", True))


def info_text(version_line: str, main: Model, builder: Model | None, prices: dict, provider: str,
              admin: bool, changes: str = "") -> str:
    """Answer to "Welche Version/welches Modell?" (also /version)."""
    out = [version_line, f"🧠 KI ({provider}): {main.text(prices if admin else {})}"]
    if admin and builder and builder.name != main.name:
        out.append(f"🛠 Werkzeug-KI: {builder.text(prices)}")
    if changes:
        out.append(changes)
    return "\n".join(out)


def _ago(raw: str, now: datetime) -> str:
    try:
        d = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return "nie"
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    s = (now - d).total_seconds()
    if s < 3600:
        return f"vor {max(int(s // 60), 1)} Min."
    if s < 86400:
        return f"vor {int(s // 3600)} Std."
    return f"vor {int(s // 86400)} Tagen"


def _usd(x: float) -> str:
    return f"{x:.2f} $".replace(".", ",") if x >= 0.01 or not x else f"{x * 100:.2f} ct".replace(".", ",")


def dashboard(*, version_line: str, models: list[str], users: Any, pending_approvals: int,
              entries: list[activity.Entry], events: list[guard.Event], monitor_status: str,
              recipes: int, now: datetime | None = None) -> str:
    """entries: activity of the last 7 days, events: guard events of the last 7 days."""
    now = now or datetime.now(timezone.utc)
    us = list(users.users.values())
    for u in us:
        u.usage.roll()
    users.total.roll()
    today = now.strftime("%Y-%m-%d")
    active = [u for u in us if u.usage.day == today and u.usage.day_requests]
    pending = [u for u in us if u.role == PENDING]
    blocked = [u for u in us if u.role == BLOCKED]
    tot = users.total
    out = ["🛠 <b>Verwaltung</b>", html.escape(version_line)] + [html.escape(m) for m in models]
    out.append(f"\n👥 <b>Nutzer</b>: {sum(1 for u in us if u.role in (ADMIN, USER))} freigeschaltet · "
               f"heute aktiv {len(active)}"
               + (f" · ⏳ {len(pending)} warten" if pending else "") + (f" · 🚫 {len(blocked)} gesperrt" if blocked else ""))
    budget = users.total_limits.get("kosten")
    money = f"💰 <b>Monat</b>: {_usd(tot.month_cost)} · {tot.month_requests} Anfragen"
    if budget:
        money += f" · {tot.month_cost / budget * 100:.0f} % vom Budget ({_usd(budget)})"
    out.append(money)
    day = now - timedelta(days=1)
    recent = [e for e in entries if e.at >= day]
    bad = [e for e in recent if e.outcome in (activity.PROBLEM, activity.ERROR)]
    asked = [e for e in recent if e.outcome not in (activity.TALK, activity.ACTION)]
    if asked:
        ok = sum(1 for e in asked if e.outcome == activity.OK)
        out.append(f"📊 <b>24 Std.</b>: {len(asked)} Fragen, {ok * 100 // len(asked)} % geklappt"
                   + (f" · ⚠️ {len(bad)} Probleme" if bad else ""))
    todo = []
    if pending:
        todo.append(f"{len(pending)} Zugangsanfrage{'n' if len(pending) != 1 else ''}")
    if pending_approvals:
        todo.append(f"{pending_approvals} {'Wünsche' if pending_approvals != 1 else 'Wunsch'} zur Freigabe")
    blocks = [e for e in events if e.kind == guard.BLOCK]
    warns = [e for e in events if e.kind == guard.WARN]
    if blocks or warns:
        todo.append(f"{len(blocks)} Sperren, {len(warns)} Warnungen (7 Tage)")
    out.append("\n📌 <b>Offen</b>: " + (" · ".join(todo) if todo else "nichts 👍"))
    out.append(html.escape(monitor_status))
    if recipes:
        out.append(f"🛠 {recipes} selbstgebaute Werkzeug{'e' if recipes != 1 else ''}")
    return "\n".join(out)


def user_card(u: Any, users: Any, requests: list[dict], entries: list[activity.Entry], events: list[guard.Event],
              extra: list[str], tz: tzinfo, now: datetime | None = None) -> str:
    """Everything about one user. entries/events: last 30 days of this user."""
    now = now or datetime.now(timezone.utc)
    u.usage.roll()
    role = {ADMIN: "👑 Admin", USER: "👤 Nutzer", PENDING: "⏳ wartet auf Freigabe", BLOCKED: "🚫 gesperrt"}
    out = [f"👤 <b>{html.escape(u.name)}</b> <code>{u.id}</code> · {role.get(u.role, u.role)}",
           f"Dabei seit {_since(u.since, tz)} · zuletzt aktiv {_ago(u.usage.last_active, now)}"]
    us = u.usage
    out.append(f"\n💬 Anfragen: heute {us.day_requests} · Monat {us.month_requests} · gesamt {us.total_requests}")
    out.append(f"💰 Kosten: Monat {_usd(us.month_cost)} · gesamt {_usd(us.total_cost)} · "
               + f"{us.month_tokens:,} Tokens im Monat".replace(",", "."))
    own = u.limits
    lim = users.limits_for(u.id)
    if u.role == ADMIN:
        out.append("🎚 Limits: keine (Admin) · Qualität: unbegrenzt")
    else:
        lim_txt = ", ".join(limit_text(k, v) + ("" if k in own else " (Standard)") for k, v in lim.items()) or "keine"
        cap = users.max_quality(u.id)
        cap_txt = quality_label(cap) + ("" if u.quality else " (Standard)") if cap else "unbegrenzt"
        out.append(f"🎚 Limits: {html.escape(lim_txt)}\n📺 Höchstqualität: {cap_txt}")
        perms = [PERMS.get(p, p) for p in getattr(u, "perms", [])]
        out.append("🔐 Darf zusätzlich: " + (html.escape("; ".join(perms)) if perms else "nichts (nur suchen und hinzufügen)"))
    mine = [r for r in requests if r.get("chat_id") == u.id]
    if mine:
        kinds = Counter(r["kind"] for r in mine)
        out.append(f"\n➕ Gewünscht: {len(mine)} ({kinds.get('movie', 0)} Filme, {kinds.get('series', 0)} Serien)")
        out.append("   Zuletzt: " + ", ".join(html.escape(r["title"]) for r in mine[-5:][::-1]))
    asked = [e for e in entries if e.outcome not in (activity.TALK, activity.ACTION)]
    if asked:
        c = Counter(e.outcome for e in asked)
        out.append(f"📊 30 Tage: {len(asked)} Fragen · " + " · ".join(
            f"{activity.ICONS[k]} {n}" for k, n in c.most_common()))
    if events:
        out.append("🚨 " + " · ".join(f"{e.at.astimezone(tz):%d.%m.} {'Sperre' if e.kind == guard.BLOCK else 'Warnung' if e.kind == guard.WARN else 'entsperrt'}: {html.escape(e.detail)}"
                                     for e in events[-3:]))
    out += extra
    return "\n".join(out)


def _since(raw: str, tz: tzinfo) -> str:
    try:
        d = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return "?"
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return f"{d.astimezone(tz):%d.%m.%Y}"
