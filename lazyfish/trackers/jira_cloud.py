"""Jira Cloud (REST API v3) tracker client.

Scope: Atlassian-hosted Jira only. Jira Server / Data Center uses different
endpoints and authentication and belongs in a separate module (Q7).

Two things drive most of the code here:

* No project key, status name or field id is hard-coded. Workflow state names
  differ between teams ("To Do", "Sprint Ready", "Ready for Dev"), so the whole
  query comes from the user's config.
* Every field access goes through `.get()` and reports which Jira field was
  missing. A KeyError deep in a mapping function is unactionable for someone
  whose instance has an unusual field configuration (R5).
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import TrackerError
from .base import Attachment, Comment, Ticket

SEARCH_FIELDS = [
    "summary",
    "description",
    "status",
    "priority",
    "issuetype",
    "reporter",
    "assignee",
    "labels",
    "created",
    "updated",
    "attachment",
]

MAX_COMMENTS = 50


# --------------------------------------------------------------------------- #
# Atlassian Document Format -> plain text
# --------------------------------------------------------------------------- #


def _attrs(node: Any) -> dict[str, Any]:
    value = node.get("attrs") if isinstance(node, dict) else None
    return value if isinstance(value, dict) else {}


def _children(node: Any) -> list[Any]:
    value = node.get("content") if isinstance(node, dict) else None
    return value if isinstance(value, list) else []


def _render_inline(node: Any) -> str:
    """Flatten an inline ADF node. Text is never altered, only decorated."""
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = _attrs(node)

    if kind == "text":
        text = node.get("text", "")
        for mark in node.get("marks") or []:
            if not isinstance(mark, dict):
                continue
            mark_type = mark.get("type")
            if mark_type == "code":
                text = f"`{text}`"
            elif mark_type == "link":
                href = _attrs(mark).get("href")
                if href and href != text:
                    text = f"{text} ({href})"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return attrs.get("text") or "@unknown"
    if kind == "emoji":
        return attrs.get("text") or attrs.get("shortName") or ""
    if kind == "date":
        return str(attrs.get("timestamp", ""))
    if kind == "status":
        return str(attrs.get("text", ""))
    if kind in ("inlineCard", "blockCard"):
        return str(attrs.get("url", ""))
    if kind in ("media", "mediaInline"):
        name = attrs.get("alt") or attrs.get("id") or "file"
        return f"[attachment: {name}]"
    return "".join(_render_inline(child) for child in _children(node))


def _prefix_lines(text: str, first: str, rest: str) -> str:
    lines = text.split("\n")
    out = [f"{first}{lines[0]}"]
    out.extend(f"{rest}{line}" for line in lines[1:])
    return "\n".join(out)


def _render_blocks(node: Any) -> list[str]:
    """Turn a block-level ADF node into a list of block strings."""
    if not isinstance(node, dict):
        return []
    kind = node.get("type")
    attrs = _attrs(node)

    if kind in ("doc", "mediaGroup", "mediaSingle"):
        blocks: list[str] = []
        for child in _children(node):
            blocks.extend(_render_blocks(child))
        return blocks
    if kind == "paragraph":
        text = "".join(_render_inline(child) for child in _children(node))
        return [text] if text.strip() else []
    if kind == "heading":
        level = int(attrs.get("level", 1) or 1)
        text = "".join(_render_inline(child) for child in _children(node))
        return [f"{'#' * max(1, min(level, 6))} {text}"] if text.strip() else []
    if kind == "codeBlock":
        language = attrs.get("language") or ""
        body = "".join(_render_inline(child) for child in _children(node))
        return [f"```{language}\n{body}\n```"]
    if kind == "rule":
        return ["---"]
    if kind in ("media", "mediaInline"):
        return [_render_inline(node)]
    if kind == "blockquote":
        inner = _join_blocks(_children(node))
        return [_prefix_lines(inner, "> ", "> ")] if inner else []
    if kind == "panel":
        inner = _join_blocks(_children(node))
        label = attrs.get("panelType") or "note"
        return [_prefix_lines(inner, f"[{label}] ", "    ")] if inner else []
    if kind in ("expand", "nestedExpand"):
        title = attrs.get("title") or "details"
        inner = _join_blocks(_children(node))
        return [f"{title}:\n{inner}" if inner else f"{title}:"]
    if kind in ("bulletList", "orderedList"):
        ordered = kind == "orderedList"
        start = int(attrs.get("order", 1) or 1)
        items: list[str] = []
        for index, item in enumerate(_children(node)):
            inner = _join_blocks(_children(item))
            marker = f"{start + index}. " if ordered else "- "
            items.append(_prefix_lines(inner, marker, " " * len(marker)))
        return ["\n".join(items)] if items else []
    if kind == "table":
        rows: list[str] = []
        for row in _children(node):
            cells = [_join_blocks(_children(cell)).replace("\n", " ") for cell in _children(row)]
            rows.append("| " + " | ".join(cells) + " |")
        return ["\n".join(rows)] if rows else []

    inner = _join_blocks(_children(node))
    return [inner] if inner else []


def _join_blocks(nodes: list[Any]) -> str:
    blocks: list[str] = []
    for node in nodes:
        blocks.extend(_render_blocks(node))
    return "\n\n".join(block for block in blocks if block)


def render_adf(value: Any) -> str:
    """Render an Atlassian Document Format value as plain text.

    Accepts a plain string too: some instances and some API versions return
    wiki markup or plain text where v3 normally returns ADF.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _join_blocks([value])
    if isinstance(value, list):
        return _join_blocks(value)
    return str(value)


# --------------------------------------------------------------------------- #
# Field mapping
# --------------------------------------------------------------------------- #


def _named(value: Any) -> str | None:
    """Jira wraps most enumerations as {"name": ...}; users as displayName."""
    if isinstance(value, dict):
        for key in ("name", "displayName", "value"):
            if isinstance(value.get(key), str):
                return value[key]
    if isinstance(value, str):
        return value
    return None


class JiraCloudClient:
    """TrackerClient implementation for Jira Cloud.

    Takes plain values, not a configuration object. The client does not know
    that profiles or configuration files exist, which is what keeps a tracker
    implementation testable without any of that machinery.
    """

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        query: str,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.query = query
        self.timeout_seconds = timeout_seconds
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                base_url=self.base_url,
                auth=(email, api_token),
                timeout=timeout_seconds,
                headers={"Accept": "application/json"},
            )
            self._owns_client = True

    # -- plumbing --------------------------------------------------------- #

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise TrackerError(
                f"Cannot reach the tracker at {self.base_url}: {exc}\n"
                f"Check [tracker] base_url and your network connection."
            ) from exc
        return response

    def _json(self, response: httpx.Response, context: str) -> dict[str, Any]:
        self._raise_for_status(response, context)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TrackerError(
                f"{context}: the tracker returned a non-JSON response "
                f"(HTTP {response.status_code}). This usually means base_url points "
                f"at something other than a Jira Cloud site."
            ) from exc
        if not isinstance(payload, dict):
            raise TrackerError(f"{context}: expected a JSON object, got {type(payload).__name__}.")
        return payload

    def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        if response.status_code < 400:
            return
        detail = self._error_detail(response)
        if response.status_code == 401:
            raise TrackerError(
                f"{context}: the tracker rejected the credentials (HTTP 401).\n"
                f"Two things produce this, and they look identical from here:\n"
                f"  - the email or api_token for this profile is wrong; check the "
                f"credentials file\n"
                f"  - the token expired. Atlassian API tokens last at most 365 days, "
                f"and the day one expires this is the only symptom.\n"
                f"Issue a new token at id.atlassian.com -> Security -> API tokens, "
                f"for {self.base_url}.{detail}"
            )
        if response.status_code == 403:
            raise TrackerError(
                f"{context}: access denied (HTTP 403). The account may lack "
                f"permission for this project.{detail}"
            )
        if response.status_code == 404:
            raise TrackerError(f"{context}: not found (HTTP 404).{detail}")
        raise TrackerError(f"{context}: HTTP {response.status_code}.{detail}")

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            text = response.text.strip()
            return f"\nResponse: {text[:400]}" if text else ""
        if isinstance(payload, dict):
            messages = payload.get("errorMessages") or []
            errors = payload.get("errors") or {}
            parts = [str(item) for item in messages if item]
            parts.extend(f"{key}: {value}" for key, value in errors.items())
            if parts:
                return "\nTracker said: " + "; ".join(parts)
        return ""

    # -- TrackerClient ---------------------------------------------------- #

    def list_candidates(self, limit: int = 5) -> list[Ticket]:
        """Run the configured JQL and return up to `limit` tickets, best first."""
        body = {
            "jql": self.query,
            "maxResults": limit,
            "fields": SEARCH_FIELDS,
        }
        response = self._request("POST", "/rest/api/3/search/jql", json=body)

        if response.status_code in (404, 405, 410):
            # Older sites still serve the retired GET /rest/api/3/search.
            response = self._request(
                "GET",
                "/rest/api/3/search",
                params={
                    "jql": self.query,
                    "maxResults": limit,
                    "fields": ",".join(SEARCH_FIELDS),
                },
            )
        if response.status_code == 400:
            raise TrackerError(
                "The tracker rejected the configured query (HTTP 400).\n"
                "Check the [tracker] query value in your config; status names and "
                "project keys are instance-specific." + self._error_detail(response)
            )

        payload = self._json(response, "Searching for candidate tickets")
        issues = payload.get("issues")
        if issues is None:
            raise TrackerError(
                "Search response contained no 'issues' key. Expected a Jira Cloud "
                "search result; check that base_url points at a Jira Cloud site."
            )
        if not isinstance(issues, list):
            raise TrackerError("Search response field 'issues' was not a list.")
        return [self._to_ticket(issue) for issue in issues]

    def fetch(self, key: str) -> Ticket:
        """Fetch one ticket including its comments."""
        response = self._request(
            "GET",
            f"/rest/api/3/issue/{key}",
            params={"fields": ",".join(SEARCH_FIELDS)},
        )
        if response.status_code == 404:
            raise TrackerError(
                f"Ticket {key} not found, or the account cannot see it.\nChecked {self.base_url}."
            )
        payload = self._json(response, f"Fetching ticket {key}")
        comments = self._fetch_comments(key)
        return self._to_ticket(payload, comments=comments)

    def download_attachment(self, attachment: Attachment) -> bytes:
        response = self._request("GET", attachment.url, follow_redirects=True)
        self._raise_for_status(response, f"Downloading attachment {attachment.filename}")
        return response.content

    # -- mapping ---------------------------------------------------------- #

    def _fetch_comments(self, key: str) -> tuple[Comment, ...]:
        response = self._request(
            "GET",
            f"/rest/api/3/issue/{key}/comment",
            params={"maxResults": MAX_COMMENTS, "orderBy": "created"},
        )
        payload = self._json(response, f"Fetching comments for {key}")
        raw_comments = payload.get("comments")
        if not isinstance(raw_comments, list):
            return ()
        return tuple(
            Comment(
                author=_named(item.get("author")) or "unknown",
                created=str(item.get("created") or ""),
                body=render_adf(item.get("body")),
            )
            for item in raw_comments
            if isinstance(item, dict)
        )

    def _to_ticket(self, issue: dict[str, Any], comments: tuple[Comment, ...] = ()) -> Ticket:
        key = issue.get("key")
        if not key:
            raise TrackerError(
                "A ticket in the tracker response had no 'key' field. "
                "The response shape is not what lazyfish expects from Jira Cloud."
            )
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            raise TrackerError(
                f"Ticket {key} came back without a 'fields' object. "
                f"Check that the account may read this issue's fields."
            )
        title = fields.get("summary")
        if not isinstance(title, str):
            raise TrackerError(
                f"Ticket {key} has no readable 'summary' field. This is usually a "
                f"field-level permission or field-configuration difference in your "
                f"Jira instance."
            )

        labels = fields.get("labels")
        return Ticket(
            key=str(key),
            title=title,
            description=render_adf(fields.get("description")),
            url=f"{self.base_url}/browse/{key}",
            status=_named(fields.get("status")),
            priority=_named(fields.get("priority")),
            issue_type=_named(fields.get("issuetype")),
            reporter=_named(fields.get("reporter")),
            assignee=_named(fields.get("assignee")),
            labels=tuple(str(item) for item in labels) if isinstance(labels, list) else (),
            created=fields.get("created"),
            updated=fields.get("updated"),
            comments=comments,
            attachments=self._to_attachments(fields.get("attachment")),
            raw=issue,
        )

    @staticmethod
    def _to_attachments(value: Any) -> tuple[Attachment, ...]:
        if not isinstance(value, list):
            return ()
        result: list[Attachment] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            size = item.get("size")
            result.append(
                Attachment(
                    filename=str(item.get("filename") or "unnamed"),
                    mime_type=str(item.get("mimeType") or "application/octet-stream"),
                    size_bytes=size if isinstance(size, int) else 0,
                    url=str(item.get("content") or ""),
                    author=_named(item.get("author")),
                    created=item.get("created"),
                )
            )
        return tuple(result)
