"""Conversation loop: user message -> model -> tools -> model -> reply."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .llm import LLMError, LLMProvider, Turn
from .state import utcnow
from .tools import ToolRunner

log = logging.getLogger(__name__)

MAX_STEPS = 8

SYSTEM_PROMPT = """\
Du bist ein Telegram-Assistent für eine private Mediathek. Du steuerst Radarr (Filme) und \
Sonarr (Serien) über Werkzeuge. Antworte in der Sprache des Nutzers, kurz und freundlich, \
ohne Markdown-Tabellen (einfacher Text, Aufzählungen mit "•" sind ok).

So gehst du vor:
1. Erkenne, ob der Nutzer einen Film oder eine Serie möchte. Ist das unklar (z. B. ein Titel, den \
es als Film und als Serie gibt), frag nach.
2. Suche immer zuerst mit search_movies bzw. search_series. Rate niemals IDs.
3. Gibt es mehrere plausible Treffer (gleicher Titel, Remakes, verschiedene Jahre), füge NICHTS \
hinzu, sondern zeig die Kandidaten nummeriert mit Jahr und einem halben Satz Beschreibung und \
frag, welcher gemeint ist. Ist ein Treffer eindeutig, füge ihn direkt hinzu.
4. Bei Serien: Sagt der Nutzer nicht, welche Staffeln, nimm alle (monitor "all"). Sagt er \
"neueste Staffel", nutze seasons mit der höchsten Staffelnummer.
5. Ist etwas schon in der Bibliothek: Mit Datei -> sag das. Ohne Datei -> biete an, die Suche \
neu zu starten (search_existing), oder tu es, wenn der Nutzer das ausdrücklich will.
6. Qualität: Setze quality_profile nur, wenn der Nutzer ausdrücklich eine Qualität nennt \
(z. B. "in 4K", "1080p reicht") – dann vorher list_quality_profiles aufrufen und das passende \
Profil nehmen. Sonst quality_profile weglassen; der Bot fragt dann selbst per Buttons nach.
7. Nach dem Hinzufügen: Bestätige kurz und sag, dass eine Nachricht kommt, sobald der Download \
startet und wenn er fertig ist. Den Download-Fortschritt erfährt man mit download_status.
8. Meldet ein Werkzeug einen Fehler, erkläre ihn verständlich. Erfinde keine Ergebnisse.
Wenn der Nutzer etwas will, das du mit den Werkzeugen nicht kannst, sag das ehrlich.
"""


@dataclass
class Conversation:
    history: list[Turn] = field(default_factory=list)
    last_used: datetime = field(default_factory=utcnow)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Agent:
    def __init__(self, provider: LLMProvider, tools: ToolRunner, idle_minutes: int, max_messages: int):
        self.provider = provider
        self.tools = tools
        self.idle = timedelta(minutes=idle_minutes)
        self.max_messages = max_messages
        self.convs: dict[int, Conversation] = {}

    def reset(self, chat_id: int) -> None:
        self.convs.pop(chat_id, None)

    async def handle(self, chat_id: int, text: str) -> str:
        conv = self.convs.setdefault(chat_id, Conversation())
        async with conv.lock:
            # Start fresh after a pause or when the thread gets long. We reset the whole
            # history instead of trimming it, so provider-side reasoning state stays valid.
            if utcnow() - conv.last_used > self.idle or len(conv.history) > self.max_messages:
                conv.history = []
            conv.last_used = utcnow()

            work = list(conv.history)
            work.append(Turn(role="user", text=text))
            replies: list[str] = []
            try:
                for _ in range(MAX_STEPS):
                    turn = await self.provider.complete(SYSTEM_PROMPT, work, self.tools.specs)
                    work.append(turn)
                    if turn.text:
                        replies.append(turn.text)
                    if not turn.tool_calls:
                        break
                    log.info("chat %s tools: %s", chat_id, [(c.name, c.args) for c in turn.tool_calls])
                    results = [await self.tools.run(c, chat_id) for c in turn.tool_calls]
                    work.append(Turn(role="tool", tool_results=results))
                else:
                    replies.append("(Ich habe zu viele Schritte gebraucht und abgebrochen. Formulier es bitte nochmal anders.)")
            except LLMError as e:
                # Drop the failed exchange so the history stays consistent.
                return f"⚠️ {e}"

            conv.history = work
            return "\n\n".join(replies) or "Erledigt."
