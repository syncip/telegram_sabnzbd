"""💡 Limit advice: look at the last days' AI use and suggest limits with a safety margin.

Total (whole bot): tokens and costs per day and per month. Standard per user: tokens and messages per day.
The margin is bigger the less we know (few days of data) and grows with the number of active users, so a
normal busy day never hits the limit but a runaway (spam, a loop, a leaked bot) is stopped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .users import ADMIN, _fmt

WINDOW = 30  # days looked at


@dataclass
class Advice:
    days: int  # days with data in the window
    active_users: int
    avg_tokens: float
    peak_tokens: int
    avg_cost: float
    peak_cost: float
    margin: float
    total: dict[str, float] = field(default_factory=dict)  # suggested total limits
    standard: dict[str, float] = field(default_factory=dict)  # suggested defaults per user


def nice(x: float) -> float:
    """Round up to 1 / 2 / 2.5 / 5 x 10^n."""
    if x <= 0:
        return 0
    exp = math.floor(math.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        v = m * 10**exp
        if v >= x - 1e-12:
            return float(round(v, 6))
    return float(10 ** (exp + 1))


def _series(usage: Any, today: date, window: int = WINDOW) -> list[dict]:
    """One entry per day of the window, from the first day with data on (missing days = 0), today last."""
    usage.roll()
    raw = dict(usage.days)
    raw[usage.day] = {"requests": usage.day_requests, "tokens": usage.day_tokens, "cost": usage.day_cost}
    start = today - timedelta(days=window - 1)
    known = sorted(d for d in raw if d >= start.isoformat())
    if not known:
        return []
    first = date.fromisoformat(known[0])
    out = []
    d = first
    while d <= today:
        out.append(raw.get(d.isoformat()) or {"requests": 0, "tokens": 0, "cost": 0.0})
        d += timedelta(days=1)
    return out


def _p(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def advise(users: Any, today: date | None = None) -> Advice:
    today = today or date.today()
    total = _series(users.total, today)
    toks = [d["tokens"] for d in total]
    costs = [d["cost"] for d in total]
    days = len(total)
    active = 0
    user_peaks_tok: list[float] = []
    user_peaks_req: list[float] = []
    for u in users.users.values():
        s = _series(u.usage, today)
        if not any(d["requests"] or d["tokens"] for d in s):
            continue
        active += 1
        if u.role != ADMIN:
            user_peaks_tok.append(max(d["tokens"] for d in s))
            user_peaks_req.append(max(d["requests"] for d in s))
    # Little data: more room. Each extra user: a bit more, they don't all peak on the same day.
    margin = (3.0 if days < 7 else 2.0 if days < 14 else 1.5) + 0.1 * max(0, active - 1)
    avg_t = sum(toks) / days if days else 0.0
    avg_c = sum(costs) / days if days else 0.0
    a = Advice(days, active, avg_t, int(max(toks, default=0)), avg_c, max(costs, default=0.0), round(margin, 2))
    if not days or not max(toks, default=0):
        return a
    day_t = max(max(toks), _p(toks, 0.9) * 1.2) * margin
    a.total["tokens_tag"] = nice(day_t)
    a.total["tokens"] = nice(max(avg_t * 30 * margin, day_t * 10))
    if max(costs, default=0) > 0:
        day_c = max(max(costs), _p(costs, 0.9) * 1.2) * margin
        a.total["kosten_tag"] = nice(day_c)
        a.total["kosten"] = nice(max(avg_c * 30 * margin, day_c * 10))
    if user_peaks_tok:
        a.standard["tokens_tag"] = nice(max(user_peaks_tok) * margin)
        a.standard["tag"] = nice(max(max(user_peaks_req) * margin, 10))
    return a


def _n(x: float) -> str:
    return f"{int(x):,}".replace(",", ".")


def render(a: Advice, users: Any) -> str:
    lines = ["💡 Limit-Empfehlung"]
    if not a.days or not a.total:
        lines.append("Noch zu wenig Verbrauch, um etwas zu empfehlen. Frag mich in ein paar Tagen nochmal.")
        return "\n".join(lines)
    lines.append(f"Grundlage: {a.days} Tag{'e' if a.days != 1 else ''}, {a.active_users} aktive Nutzer. "
                 f"Im Schnitt {_n(a.avg_tokens)} Tokens/Tag" + (f" ({a.avg_cost:.3f} $)".replace(".", ",") if a.avg_cost else "")
                 + f", am meisten {_n(a.peak_tokens)}" + (f" ({a.peak_cost:.3f} $)".replace(".", ",") if a.peak_cost else "") + ".")
    lines.append(f"Sicherheitsaufschlag: ×{a.margin:g}".replace(".", ",")
                 + (" (wenig Daten)" if a.days < 7 else "") + ".")

    def row(key: str, value: float, current: float | None) -> str:
        now = f" (jetzt: {_fmt(key, current)})" if current else " (jetzt: keins)"
        return f"• {_fmt(key, value)}{now}"

    lines.append("\nGesamt (ganzer Bot)")
    lines += [row(k, v, users.total_limits.get(k)) for k, v in a.total.items()]
    if a.standard:
        lines.append("\nStandard pro Nutzer (Admins ausgenommen)")
        lines += [row(k, v, users.defaults.get(k)) for k, v in a.standard.items()]
    lines.append("\nIst ein Gesamtlimit erreicht, antwortet der Bot bis zum nächsten Tag/Monat niemandem mehr "
                 "(auch dir nicht). Ab 80 % sage ich dir vorher Bescheid.")
    return "\n".join(lines)


def apply(a: Advice, users: Any, which: str = "all") -> str:
    """which: all | total | standard. Returns what was set."""
    done = []
    if which in ("all", "total"):
        for k, v in a.total.items():
            users.set_limit("gesamt", k, v)
            done.append(f"Gesamt: {_fmt(k, v)}")
    if which in ("all", "standard"):
        for k, v in a.standard.items():
            users.set_limit("standard", k, v)
            done.append(f"Standard: {_fmt(k, v)}")
    return "✅ Übernommen:\n" + "\n".join(f"• {d}" for d in done) if done else "Nichts zu übernehmen."
