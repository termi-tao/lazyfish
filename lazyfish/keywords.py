"""Candidate search terms extracted from ticket text.

Deliberately mechanical and language-agnostic: identifier shapes, path shapes
and quoted spans, nothing else. No NLP, no language detection, no stemming.

The consequence, stated plainly because it matters when reading the results, is
that this is never better than "adequate" in any single language (R2). The
templates label the output as mechanical search hits for exactly that reason.
"""

from __future__ import annotations

import re

MAX_KEYWORDS = 8

# Terms that appear in nearly every ticket and match nearly every file.
STOPWORDS = frozenset(
    {
        "and",
        "api",
        "app",
        "are",
        "com",
        "config",
        "data",
        "error",
        "false",
        "file",
        "for",
        "from",
        "get",
        "http",
        "https",
        "index",
        "into",
        "issue",
        "jira",
        "log",
        "main",
        "net",
        "new",
        "none",
        "not",
        "null",
        "org",
        "page",
        "set",
        "should",
        "test",
        "that",
        "the",
        "this",
        "todo",
        "true",
        "url",
        "use",
        "user",
        "value",
        "when",
        "with",
        "www",
    }
)

_QUOTED = re.compile(r"[`\"']([^`\"'\n]{2,60})[`\"']")
_PATHLIKE = re.compile(r"\b[\w.-]+/[\w./-]+\b")
_DOTTED = re.compile(r"\b[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+\b")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CAMEL = re.compile(r"\b[A-Za-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b")

# Quoted spans are only kept when they look like code, not prose.
_IDENTIFIER_SHAPED = re.compile(r"^[\w./-]{2,60}$")


def _accept(token: str) -> bool:
    token = token.strip()
    if len(token) < 3 or len(token) > 60:
        return False
    if token.lower() in STOPWORDS:
        return False
    # Pure numbers, version strings and dates are never useful search terms.
    return not all(char.isdigit() or char in "._-" for char in token)


def extract_keywords(*texts: str, limit: int = MAX_KEYWORDS) -> list[str]:
    """Return up to `limit` search terms, most specific shapes first.

    Ordering is by shape, then by first appearance: a path mentioned in a ticket
    is a stronger signal than a CamelCase word that happens to be a product name
    in the title.
    """
    blob = "\n".join(text for text in texts if text)
    if not blob.strip():
        return []

    ordered: list[str] = []
    seen: set[str] = set()

    def add(candidates: list[str]) -> None:
        for candidate in candidates:
            token = candidate.strip().strip(".,;:!?()[]{}")
            if not _accept(token):
                continue
            fold = token.lower()
            if fold in seen:
                continue
            seen.add(fold)
            ordered.append(token)

    add([match for match in _QUOTED.findall(blob) if _IDENTIFIER_SHAPED.match(match)])
    add(_PATHLIKE.findall(blob))
    add(_SNAKE.findall(blob))
    add(_CAMEL.findall(blob))
    add(_DOTTED.findall(blob))

    return ordered[:limit]
