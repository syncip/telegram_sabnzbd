"""🗓 Monthly report for the admins (1st of the month, or /monatsbericht). Built in code from what the bot
already keeps: usage per user, requests, activity log, spam/warning events and self-built tools."""

from __future__ import annotations

import html
from collections import Counter
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

from . import activity, guard
from .users import ADMIN, BLOCKED, PENDING

MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]


def previous_month(today: date) -> str:
    first = today.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def bounds(month: str, tz: tzinfo) -> tuple[datetime, datetime]:
    y, m = map(int, month.split("-"))
    start = datetime(y, m, 1, tzinfo=tz)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def month_label(month: str) -> str:
    y, m = month.split("-")
    return f"{MONTHS[int(m) - 1]} {y}"


def usage_of(u: Any, month: str) -> dict:
    """Requests/tokens/cost of one Usage in a month (finished months are archived in u.months)."""
    u.roll()
    if u.month == month:
        return {"requests": u.month_requests, "tokens": u.month_tokens, "cost": u.month_cost}
    return dict(u.months.get(month) or {})


def _usd(x: float) -> str:
    return f"{x:.2f} $".replace(".", ",") if x >= 0.01 or not x else f"{x * 100:.2f} ct".replace(".", ",")


def _delta(now: float, before: float) -> str:
    if not before:
        return ""
    pct = (now - before) / before * 100
    return f" ({'+' if pct >= 0 else ''}{pct:.0f} % zum Vormonat)"


def _at(raw: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def render(month: str, users: Any, requests: list[dict], entries: list[activity.Entry], events: list[guard.Event],
           recipes: list[Any], tz: tzinfo) -> str:
    start, end = bounds(month, tz)
    before = (start.astimezone(tz).date() - timedelta(days=1)).strftime("%Y-%m")
    name = users.name
    out = [f"🗓 <b>Monatsbericht {month_label(month)}</b>"]

    tot, prev = usage_of(users.total, month), usage_of(users.total, before)
    cost = tot.get("cost", 0) or 0
    out.append(f"\n💰 <b>Kosten</b>: {_usd(cost)}{_delta(cost, prev.get('cost', 0) or 0)}\n"
               f"   {tot.get('requests', 0)} Anfragen{_delta(tot.get('requests', 0), prev.get('requests', 0))} · "
               + f"{(tot.get('tokens', 0) or 0):,} Tokens".replace(",", "."))

    per_user = []
    for u in users.users.values():
        d = usage_of(u.usage, month)
        if d.get("requests") or d.get("cost"):
            per_user.append((u, d))
    per_user.sort(key=lambda x: -(x[1].get("cost", 0) or 0))
    if per_user:
        out.append("\n👥 <b>Nutzer</b> (nach Kosten)")
        out += [f"   {html.escape(u.name)}: {d.get('requests', 0)} Anfragen · {_usd(d.get('cost', 0) or 0)}"
                for u, d in per_user[:8]]
    new = [u for u in users.users.values() if (at := _at(u.since)) and start <= at < end and u.role != ADMIN]
    blocked = [u for u in users.users.values() if u.role == BLOCKED]
    waiting = [u for u in users.users.values() if u.role == PENDING]
    facts = []
    if new:
        facts.append(f"neu: {', '.join(html.escape(u.name) for u in new[:6])}")
    if blocked:
        facts.append(f"gesperrt: {', '.join(html.escape(u.name) for u in blocked[:6])}")
    if waiting:
        facts.append(f"{len(waiting)} warten auf Freigabe")
    if facts:
        out.append("   " + " · ".join(facts))

    reqs = [r for r in requests if (at := _at(r.get("at", ""))) and start <= at < end]
    if reqs:
        kinds = Counter(r["kind"] for r in reqs)
        by_user = Counter(r["chat_id"] for r in reqs)
        out.append(f"\n➕ <b>Hinzugefügt</b>: {len(reqs)} Titel ({kinds.get('movie', 0)} Filme, "
                   f"{kinds.get('series', 0)} Serien)")
        out.append("   Am meisten: " + ", ".join(f"{html.escape(name(uid))} {n}" for uid, n in by_user.most_common(3)))
        popular = [(t, n) for t, n in Counter(r["title"] for r in reqs).most_common(3) if n > 1]
        if popular:
            out.append("   Mehrfach gewünscht: " + ", ".join(f"{html.escape(t)} ({n}×)" for t, n in popular))

    month_entries = [e for e in entries if start <= e.at < end]
    asked = [e for e in month_entries if e.outcome not in (activity.TALK, activity.ACTION)]
    if asked:
        ok = sum(1 for e in asked if e.outcome == activity.OK)
        bad = Counter(e.outcome for e in asked if e.outcome in (activity.PROBLEM, activity.ERROR, activity.NOTHING))
        out.append(f"\n📊 <b>Erfolgsquote</b>: {ok * 100 // len(asked)} % von {len(asked)} Fragen geklappt")
        if bad:
            out.append("   " + " · ".join(f"{activity.ICONS[k]} {n}× {activity.LABELS[k]}" for k, n in bad.most_common()))
        problems = Counter(e.detail[:80] for e in month_entries if e.outcome in (activity.PROBLEM, activity.ERROR)
                           and e.detail)
        for detail, n in problems.most_common(3):
            out.append(f"   • {n}× {html.escape(detail)}")

    month_events = [e for e in events if start <= e.at < end]
    if month_events:
        blocks = [e for e in month_events if e.kind == guard.BLOCK]
        warns = [e for e in month_events if e.kind == guard.WARN]
        out.append(f"\n🚨 <b>Schutz</b>: {len(blocks)} vorsorgliche Sperren, {len(warns)} Warnungen")
        for e in blocks[:5]:
            out.append(f"   🚫 {e.at.astimezone(tz):%d.%m.} {html.escape(name(e.user_id))}: {html.escape(e.detail)}")
        for uid, n in Counter(e.user_id for e in warns).most_common(3):
            out.append(f"   ⚠️ {html.escape(name(uid))}: {n} Warnung{'en' if n != 1 else ''}")
    else:
        out.append("\n🚨 <b>Schutz</b>: keine Sperren oder Warnungen 👍")

    built = [r for r in recipes if (at := _at(r.created)) and start <= at < end]
    if built:
        cost_b = sum(r.cost for r in built)
        out.append(f"\n🛠 <b>Neue Werkzeuge</b>: {len(built)} ({_usd(cost_b)}): "
                   + ", ".join(html.escape(r.title) for r in built[:5]))
    used = sorted((r for r in recipes if r.uses), key=lambda r: -r.uses)[:3]
    if used:
        out.append("   Meistgenutzt: " + ", ".join(f"{html.escape(r.title)} ({r.uses}×)" for r in used))
    return "\n".join(out)
