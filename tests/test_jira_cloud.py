"""Jira Cloud client: field mapping, ADF rendering, and error shapes.

The interesting cases are the ones that come from someone else's instance:
missing fields, unexpected response shapes, the retired search endpoint. Those
are the ones that turn into an unreadable KeyError if nobody thought about them
(R5).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from lazyfish.errors import TrackerError
from lazyfish.trackers.jira_cloud import JiraCloudClient, render_adf

BASE_URL = "https://example.atlassian.net"


def make_client(handler, **overrides: Any) -> JiraCloudClient:
    """A client wired to a stub transport.

    Built from plain values: the tracker implementation never sees a profile or
    a configuration file, and this call proves it.
    """
    values: dict[str, Any] = {
        "base_url": BASE_URL,
        "email": "you@example.com",
        "api_token": "token",
        "query": 'assignee = currentUser() AND status = "Ready for Dev"',
    }
    values.update(overrides)
    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url=BASE_URL, transport=transport)
    return JiraCloudClient(client=http, **values)


def adf(*paragraphs: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
            for text in paragraphs
        ],
    }


def issue(key: str = "PROJ-1", **fields: Any) -> dict[str, Any]:
    base = {
        "summary": "Reset links expire too early",
        "description": adf("The TTL is wrong."),
        "status": {"name": "Ready for Dev"},
        "priority": {"name": "High"},
        "issuetype": {"name": "Bug"},
        "reporter": {"displayName": "Sam Reporter"},
        "assignee": {"displayName": "Alex Dev"},
        "labels": ["auth"],
        "created": "2026-01-05T09:00:00.000+0000",
        "updated": "2026-01-06T11:30:00.000+0000",
    }
    base.update(fields)
    return {"key": key, "id": "10001", "fields": base}


# --------------------------------------------------------------------------- #
# Searching
# --------------------------------------------------------------------------- #


def test_list_candidates_maps_fields_and_sends_the_configured_query() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"issues": [issue(), issue("PROJ-2")]})

    client = make_client(handler)
    tickets = client.list_candidates(limit=5)

    assert seen["method"] == "POST"
    assert seen["path"] == "/rest/api/3/search/jql"
    assert "Ready for Dev" in seen["body"]
    assert [ticket.key for ticket in tickets] == ["PROJ-1", "PROJ-2"]
    assert tickets[0].title == "Reset links expire too early"
    assert tickets[0].priority == "High"
    assert tickets[0].status == "Ready for Dev"
    assert tickets[0].assignee == "Alex Dev"
    assert tickets[0].url == f"{BASE_URL}/browse/PROJ-1"


def test_search_falls_back_to_the_retired_endpoint() -> None:
    """Sites that have not migrated still answer the old GET endpoint."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/rest/api/3/search/jql":
            return httpx.Response(404, json={"errorMessages": ["Not found"]})
        return httpx.Response(200, json={"issues": [issue()]})

    tickets = make_client(handler).list_candidates()
    assert calls == ["POST /rest/api/3/search/jql", "GET /rest/api/3/search"]
    assert tickets[0].key == "PROJ-1"


def test_a_rejected_query_points_at_the_config() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"errorMessages": ["Field 'sprint' does not exist"], "errors": {}},
        )

    with pytest.raises(TrackerError) as excinfo:
        make_client(handler).list_candidates()
    message = str(excinfo.value)
    assert "query" in message
    assert "Field 'sprint' does not exist" in message


def test_bad_credentials_offer_both_explanations() -> None:
    """401 is ambiguous between a wrong token and an expired one (R10).

    Both cost the user real time to diagnose, and only one of them is visible
    in any file, so the message has to name both.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errorMessages": ["Unauthorized"]})

    with pytest.raises(TrackerError) as excinfo:
        make_client(handler).list_candidates()
    message = str(excinfo.value)
    assert "credentials file" in message
    assert "365 days" in message
    assert "id.atlassian.com" in message
    assert BASE_URL in message


def test_a_non_json_response_is_explained() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login page</html>")

    with pytest.raises(TrackerError, match="non-JSON"):
        make_client(handler).list_candidates()


def test_a_response_without_issues_is_explained() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total": 0})

    with pytest.raises(TrackerError, match="no 'issues' key"):
        make_client(handler).list_candidates()


def test_network_failure_mentions_the_base_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nodename nor servname provided")

    with pytest.raises(TrackerError) as excinfo:
        make_client(handler).list_candidates()
    assert BASE_URL in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Fetching one ticket
# --------------------------------------------------------------------------- #


def comment(author: str, text: str, created: str = "2026-01-06T10:00:00.000+0000"):
    return {"author": {"displayName": author}, "created": created, "body": adf(text)}


def fetch_handler(
    payload: dict[str, Any] | None = None, comments: list[dict[str, Any]] | None = None
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/comment"):
            return httpx.Response(200, json={"comments": comments or []})
        return httpx.Response(200, json=payload or issue())

    return handler


def test_fetch_includes_comments() -> None:
    client = make_client(
        fetch_handler(
            comments=[
                comment("Alex Dev", "Reproduced on staging."),
                comment("Sam Reporter", "Customer is waiting."),
            ]
        )
    )
    ticket = client.fetch("PROJ-1")
    assert [item.author for item in ticket.comments] == ["Alex Dev", "Sam Reporter"]
    assert ticket.comments[0].body == "Reproduced on staging."


def test_missing_summary_names_the_field() -> None:
    payload = issue()
    del payload["fields"]["summary"]
    with pytest.raises(TrackerError) as excinfo:
        make_client(fetch_handler(payload)).fetch("PROJ-1")
    message = str(excinfo.value)
    assert "summary" in message
    assert "PROJ-1" in message


def test_missing_fields_object_is_explained() -> None:
    with pytest.raises(TrackerError, match="'fields' object"):
        make_client(fetch_handler({"key": "PROJ-1"})).fetch("PROJ-1")


def test_optional_fields_may_be_absent_or_null() -> None:
    """A minimal instance must still produce a usable ticket."""
    payload = {"key": "PROJ-9", "fields": {"summary": "Bare ticket", "assignee": None}}
    ticket = make_client(fetch_handler(payload)).fetch("PROJ-9")
    assert ticket.title == "Bare ticket"
    assert ticket.assignee is None
    assert ticket.priority is None
    assert ticket.description == ""
    assert ticket.labels == ()


def test_unknown_ticket_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})

    with pytest.raises(TrackerError, match="PROJ-404 not found"):
        make_client(handler).fetch("PROJ-404")


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #


def test_attachment_metadata_is_mapped() -> None:
    payload = issue(
        attachment=[
            {
                "filename": "server.log",
                "mimeType": "text/plain",
                "size": 2048,
                "content": f"{BASE_URL}/secure/attachment/1/server.log",
                "author": {"displayName": "Alex Dev"},
                "created": "2026-01-06T10:00:00.000+0000",
            },
            {"filename": "screenshot.png", "mimeType": "image/png", "size": 500_000},
        ]
    )
    ticket = make_client(fetch_handler(payload)).fetch("PROJ-1")
    assert [item.filename for item in ticket.attachments] == [
        "server.log",
        "screenshot.png",
    ]
    assert ticket.attachments[0].size_bytes == 2048
    assert ticket.attachments[1].url == ""
    assert ticket.attachments[0].inline_text is None


def test_download_attachment_returns_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"line one\nline two\n")

    client = make_client(handler)
    payload = issue(
        attachment=[
            {
                "filename": "server.log",
                "mimeType": "text/plain",
                "size": 18,
                "content": f"{BASE_URL}/secure/attachment/1/server.log",
            }
        ]
    )
    attachment = make_client(fetch_handler(payload)).fetch("PROJ-1").attachments[0]
    assert client.download_attachment(attachment) == b"line one\nline two\n"


# --------------------------------------------------------------------------- #
# Atlassian Document Format
# --------------------------------------------------------------------------- #


def test_adf_paragraphs_and_marks() -> None:
    document = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "See "},
                    {
                        "type": "text",
                        "text": "RESET_TOKEN_TTL",
                        "marks": [{"type": "code"}],
                    },
                    {"type": "text", "text": " in "},
                    {
                        "type": "text",
                        "text": "the runbook",
                        "marks": [{"type": "link", "attrs": {"href": "https://example.com/rb"}}],
                    },
                ],
            },
            {"type": "paragraph", "content": [{"type": "text", "text": "Second."}]},
        ],
    }
    assert render_adf(document) == (
        "See `RESET_TOKEN_TTL` in the runbook (https://example.com/rb)\n\nSecond."
    )


def test_adf_lists_and_code_blocks() -> None:
    document = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "first"}],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "second"}],
                            }
                        ],
                    },
                ],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [{"type": "text", "text": "TTL = 3600"}],
            },
        ],
    }
    assert render_adf(document) == ("- first\n- second\n\n```python\nTTL = 3600\n```")


def test_adf_mentions_and_media() -> None:
    document = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "attrs": {"text": "@Alex Dev"}},
                    {"type": "text", "text": " please look"},
                ],
            },
            {
                "type": "mediaSingle",
                "content": [{"type": "media", "attrs": {"alt": "screenshot.png", "id": "abc"}}],
            },
        ],
    }
    assert render_adf(document) == ("@Alex Dev please look\n\n[attachment: screenshot.png]")


def test_adf_accepts_plain_text_and_none() -> None:
    assert render_adf("already plain text") == "already plain text"
    assert render_adf(None) == ""


def test_adf_preserves_non_english_text_exactly() -> None:
    """The renderer decorates structure; it never touches the characters."""
    original = "登录失败，请检查 auth_service.py"
    assert render_adf(adf(original)) == original
