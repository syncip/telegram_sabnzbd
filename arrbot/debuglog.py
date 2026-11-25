"""🐞 Chat log for debugging: what the bot did for a message, step by step.

Per chat the last steps are kept in memory: the message, each AI step (model, time, tokens), every
tool call with its arguments and result, each Radarr/Sonarr request, and what Sonarr/Radarr reported
when a search command finished ("Season search completed. 0 reports downloaded.").

The chat a step belongs to travels in a context variable, so the Radarr/Sonarr layer needs no chat id
and background tasks started while answering (asyncio copies the context) still land in the right chat.
/log shows it; /debug an sends it after every answer, and late results (search finished) as they come.
"""

from __future__ import annotations

import html
from collections import deque
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Awaitable, Callable

MAX_ENTRIES = 300  # per chat
MAX_TEXT = 400  # per entry

CURRENT: ContextVar[int | None] = ContextVar("debug_chat", default=None)


@dataclass
class Entry:
    at: datetime
    request: int  # which message/button it belongs to
    icon: str
    text: str


@dataclass
class ChatLog:
    entries: deque = field(default_factory=lambda: deque(maxlen=MAX_ENTRIES))
    request: int = 0


class DebugLog:
    def __init__(self) -> None:
        self.chats: dict[int, ChatLog] = {}
        self.live: set[int] = set()  # admins who get the log after every answer (/debug an)
        # chat_id of the step, text -> sends late entries to admins with /debug an
        self.notify: Callable[[int, str], Awaitable[None]] | None = None

    def begin(self, chat_id: int, what: str, icon: str = "💬") -> Token:
        """A new message or button: the following steps belong to it. end(token) when done."""
        c = self.chats.setdefault(chat_id, ChatLog())
        c.request += 1
        self._put(chat_id, icon, what)
        return CURRENT.set(chat_id)

    @staticmethod
    def end(token: Token) -> None:
        CURRENT.reset(token)

    def add(self, icon: str, text: str, chat_id: int | None = None) -> None:
        chat_id = chat_id if chat_id is not None else CURRENT.get()
        if chat_id is not None:
            self._put(chat_id, icon, text)

    def late(self, chat_id: int, icon: str, text: str) -> Entry | None:
        """An entry that arrives after the answer (search finished, download started)."""
        return self._put(chat_id, icon, text)

    def _put(self, chat_id: int, icon: str, text: str) -> Entry:
        c = self.chats.setdefault(chat_id, ChatLog())
        text = " ".join(str(text).split())
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + " …"
        e = Entry(datetime.now(timezone.utc), c.request, icon, text)
        c.entries.append(e)
        return e

    def entries(self, chat_id: int, requests: int = 1) -> list[Entry]:
        """The steps of the last `requests` messages of this chat (plus what came in later)."""
        c = self.chats.get(chat_id)
        if not c or not c.entries:
            return []
        first = c.request - requests + 1
        return [e for e in c.entries if e.request >= first]

    def render(self, chat_id: int, tz: tzinfo, requests: int = 1, title: str = "", limit: int = 3800) -> str:
        es = self.entries(chat_id, requests)
        if not es:
            return f"🐞 {html.escape(title) + ': ' if title else ''}noch nichts aufgezeichnet."
        lines = [f"{e.at.astimezone(tz):%H:%M:%S} {e.icon} {html.escape(e.text)}" for e in es]
        head = f"🐞 <b>Log{' ' + html.escape(title) if title else ''}</b>\n"
        body = "\n".join(lines)
        if len(body) > limit:  # Telegram: max. 4096 characters – keep the end
            body = "…\n" + body[-limit:].split("\n", 1)[-1]
        return head + "<pre>" + body + "</pre>"


LOG = DebugLog()


def short(value: object, n: int = 200) -> str:
    s = " ".join(str(value).split())
    return s if len(s) <= n else s[:n] + " …"
