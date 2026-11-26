"""🔔 What each chat wants to hear about downloads ("keine Info mehr, wenn ein Download startet").

Kinds of messages:
- start: a download started (and its finish time)
- done: finished and imported (series: one summary when everything is through)
- problem: a download failed or is stuck
- nothing: a search found nothing (with the reason)
Anything else (pause over, wishes, admin notices) is always sent. Muting (/stumm) is separate and
pauses everything for a while.
"""

from __future__ import annotations

from typing import Any

KINDS = {
    "start": "⬇️ Download gestartet (mit Fertig-Zeit)",
    "done": "✅ Fertig geladen (Serien: eine Zusammenfassung, wenn alles da ist)",
    "problem": "❌ Download fehlgeschlagen oder hängt",
    "nothing": "🔍 Suche hat nichts gefunden (mit Grund)",
}


def prefs(store: Any, chat_id: int) -> dict[str, bool]:
    own = store.state.notify.get(str(chat_id)) or {}
    return {k: bool(own.get(k, True)) for k in KINDS}


def wants(store: Any, chat_id: int, kind: str) -> bool:
    if kind not in KINDS:
        return True
    return prefs(store, chat_id)[kind]


def change(store: Any, chat_id: int, **values: bool | None) -> dict[str, bool]:
    cur = prefs(store, chat_id)
    for k, v in values.items():
        if k in KINDS and v is not None:
            cur[k] = bool(v)
    # only what differs from "everything on" is stored
    off = {k: v for k, v in cur.items() if not v}
    if off:
        store.state.notify[str(chat_id)] = off
    else:
        store.state.notify.pop(str(chat_id), None)
    store.save()
    return cur


def render(store: Any, chat_id: int) -> str:
    p = prefs(store, chat_id)
    lines = ["🔔 Diese Meldungen bekommst du:"]
    lines += [f"{'✅' if p[k] else '🚫'} {label}" for k, label in KINDS.items()]
    m = store.state.muted.get(str(chat_id))
    if m:
        lines.append(f"🔕 Gerade stumm bis {m['until'][:16].replace('T', ' ')} UTC.")
    lines.append("Ändern: Buttons unten oder z. B. „keine Info mehr, wenn ein Download startet“.")
    return "\n".join(lines)


def buttons(store: Any, chat_id: int) -> list[tuple[str, str]]:
    p = prefs(store, chat_id)
    short = {"start": "Start", "done": "Fertig", "problem": "Probleme", "nothing": "Nichts gefunden"}
    return [(f"{'✅' if p[k] else '🚫'} {short[k]}", f"nt:{k}") for k in KINDS]
