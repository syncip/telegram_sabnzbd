"""Turn the model's light Markdown into Telegram HTML.

Telegram cannot render real tables, so Markdown tables become aligned
monospace blocks. Everything is HTML-escaped first, so titles with < or &
can never break the message.
"""

from __future__ import annotations

import html
import re

_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _inline(line: str) -> str:
    s = html.escape(line, quote=False)
    if m := re.match(r"^\s*#{1,6}\s+(.*)$", s):
        return f"<b>{m.group(1)}</b>"
    s = re.sub(r"^(\s*)[-*]\s+", r"\1• ", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__(.+?)__", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def table_block(rows: list[list[str]], max_col: int = 28) -> str:
    """Aligned monospace table (first row = header)."""
    rows = [[c.strip()[:max_col] for c in r] for r in rows if r]
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(ncols)]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]
    lines.insert(1, "─" * len(max(lines, key=len)))
    return "<pre>" + html.escape("\n".join(lines), quote=False) + "</pre>"


def to_html(text: str) -> str:
    out: list[str] = []
    table: list[list[str]] = []

    def flush() -> None:
        if table:
            out.append(table_block(table))
            table.clear()

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            if not _SEPARATOR.match(stripped):
                table.append(stripped.strip("|").split("|"))
            continue
        flush()
        out.append(_inline(line))
    flush()
    return "\n".join(out)


def chunks(text: str, size: int = 3500) -> list[str]:
    """Split on line breaks, never inside a Markdown table."""
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size and cur and not line.lstrip().startswith("|"):
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    while len(cur) > size:  # one absurdly long line
        parts.append(cur[:size])
        cur = cur[size:]
    parts.append(cur)
    return parts
