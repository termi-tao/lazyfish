"""Tracker-neutral data model and client protocol.

Nothing in these dataclasses is Jira-specific, and nothing here performs I/O.
Text fields hold whatever the tracker returned, character for character: no
translation, normalisation, transliteration or summarising, whatever language
the team writes in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Attachment:
    """A file attached to a ticket.

    Metadata is always captured. `inline_text` is populated only when the file
    passed both the mime allowlist and the size threshold, so a plan can note
    that a screenshot exists without the tool downloading it (R1).
    """

    filename: str
    mime_type: str
    size_bytes: int
    url: str
    author: str | None = None
    created: str | None = None
    inline_text: str | None = None
    local_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "url": self.url,
            "author": self.author,
            "created": self.created,
            "inline_text": self.inline_text,
            "local_path": self.local_path,
        }


@dataclass(frozen=True)
class Comment:
    """One comment, body already flattened to plain text."""

    author: str
    created: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        return {"author": self.author, "created": self.created, "body": self.body}


@dataclass(frozen=True)
class Ticket:
    """A ticket as lazyfish understands it."""

    key: str
    title: str
    description: str
    url: str
    status: str | None = None
    priority: str | None = None
    issue_type: str | None = None
    reporter: str | None = None
    assignee: str | None = None
    labels: tuple[str, ...] = ()
    created: str | None = None
    updated: str | None = None
    comments: tuple[Comment, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Shape written to artifacts/<KEY>/ticket.json."""
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "url": self.url,
            "status": self.status,
            "priority": self.priority,
            "issue_type": self.issue_type,
            "reporter": self.reporter,
            "assignee": self.assignee,
            "labels": list(self.labels),
            "created": self.created,
            "updated": self.updated,
            "comments": [comment.to_dict() for comment in self.comments],
            "attachments": [item.to_dict() for item in self.attachments],
        }


@runtime_checkable
class TrackerClient(Protocol):
    """What lazyfish needs from an issue tracker.

    Implementations are pure data access: they fetch and map, they do not print,
    do not touch the database and do not write to the working tree. The one
    exception is `download_attachment`, which returns bytes for the caller to
    decide about.
    """

    def list_candidates(self, limit: int = 5) -> list[Ticket]:
        """Tickets matching the configured query, best first.

        May return lightweight tickets (no comments or attachments); `fetch` is
        called once a choice has been made.
        """
        ...

    def fetch(self, key: str) -> Ticket:
        """One ticket with comments and attachment metadata."""
        ...

    def download_attachment(self, attachment: Attachment) -> bytes:
        """Raw bytes of an attachment the caller has decided to inline."""
        ...

    def close(self) -> None:
        """Release network resources."""
        ...
