"""Terminal tables whose columns line up when the text is not ASCII.

`len()` counts codepoints, and a terminal draws columns. For a tool that passes
ticket titles through verbatim - Chinese, Japanese, Korean - those two numbers
differ constantly, and a table padded by `len()` is visibly crooked on the first
non-Latin ticket. That is not an edge case here; it is the normal case for teams
this tool is meant to serve (R2).

This module never prints. It returns lines and lets the CLI decide.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

ELLIPSIS = "..."


def display_width(text: str) -> int:
    """Columns a terminal will use for `text`.

    East Asian Wide and Fullwidth characters take two columns; combining marks
    take none, since they render on top of the preceding character. Everything
    else counts as one. Ambiguous-width characters are counted as one, which is
    what a Western terminal does; a CJK-configured terminal may disagree, and
    that is a limitation worth knowing rather than pretending away.
    """
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def truncate_to_width(text: str, limit: int) -> str:
    """Shorten `text` so it occupies at most `limit` columns.

    Truncation happens on display width, not on character count, so a Chinese
    title is cut at the right place rather than at twice the intended length.
    """
    if limit <= 0:
        return ""
    if display_width(text) <= limit:
        return text
    if limit <= len(ELLIPSIS):
        return ELLIPSIS[:limit]

    budget = limit - len(ELLIPSIS)
    kept: list[str] = []
    used = 0
    for char in text:
        char_width = display_width(char)
        if used + char_width > budget:
            break
        kept.append(char)
        used += char_width
    return "".join(kept) + ELLIPSIS


def pad_to_width(text: str, width: int) -> str:
    """Left-align `text` in a field of `width` columns."""
    padding = width - display_width(text)
    return text + " " * max(0, padding)


@dataclass(frozen=True)
class Column:
    """One column: its heading and, optionally, a cap on its width."""

    heading: str
    max_width: int | None = None


def render_table(columns: list[Column], rows: list[list[str]], indent: str = "  ") -> list[str]:
    """Render a heading row plus `rows`, aligned by display width.

    The last column is never padded on the right: trailing spaces serve no
    purpose and make copied output messy.
    """
    if not rows:
        return []

    cells = [
        [
            truncate_to_width(cell, column.max_width) if column.max_width else cell
            for cell, column in zip(row, columns, strict=True)
        ]
        for row in rows
    ]

    widths = [
        max(display_width(column.heading), *(display_width(row[index]) for row in cells))
        for index, column in enumerate(columns)
    ]

    def line(values: list[str]) -> str:
        padded = [
            value if index == len(values) - 1 else pad_to_width(value, widths[index])
            for index, value in enumerate(values)
        ]
        return (indent + "  ".join(padded)).rstrip()

    return [line([column.heading for column in columns]), *(line(row) for row in cells)]
