"""Shared fixtures.

The suite uses real git repositories and real worktrees in temporary
directories, because worktree behaviour is exactly the kind of thing a mock
would get wrong. Only the tracker is stubbed.

Nothing here touches the developer's own config file or database: every test
redirects both through LAZYFISH_CONFIG and LAZYFISH_DATA_DIR.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from lazyfish.trackers.base import Attachment, Comment, Ticket

# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small git repository with one commit."""
    path = tmp_path / "target-repo"
    (path / "src" / "auth").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "src" / "auth" / "reset_token.py").write_text(
        "RESET_TOKEN_TTL = 3600\n\n\ndef build_reset_link(user_id):\n"
        "    return f'/reset/{user_id}'\n",
        encoding="utf-8",
    )
    (path / "src" / "auth" / "mailer.py").write_text(
        "def send_reset_email(address):\n    return True\n", encoding="utf-8"
    )
    (path / "README.md").write_text("# target repo\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "lazyfish tests")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial commit")
    return path


@pytest.fixture
def second_repo(tmp_path: Path) -> Path:
    """A second repository, for the multi-profile tests."""
    path = tmp_path / "other-repo"
    path.mkdir()
    (path / "index.ts").write_text("export const pageSize = 20;\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "lazyfish tests")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial commit")
    return path


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect config and state into the temporary directory."""
    config_file = tmp_path / "config" / "config.toml"
    data_dir = tmp_path / "data"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LAZYFISH_CONFIG", str(config_file))
    monkeypatch.setenv("LAZYFISH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LAZYFISH_EMAIL", "tester@example.com")
    monkeypatch.setenv("LAZYFISH_TOKEN", "not-a-real-token")
    return {"config": config_file, "data": data_dir}


def write_config(
    path: Path,
    *,
    repos: dict[str, dict[str, str]],
    base_url: str = "https://example.atlassian.net",
    query: str = "assignee = currentUser() ORDER BY priority DESC",
    email_env: str | None = "LAZYFISH_EMAIL",
    token_env: str | None = "LAZYFISH_TOKEN",
    extra_tracker: str = "",
) -> Path:
    """Write a config file. `repos` maps profile name to a table of raw TOML.

    email_env and token_env are written when given and omitted when None, which
    is what `lazyfish init` produces.
    """
    lines = [
        "[tracker]",
        'kind = "jira-cloud"',
        f'base_url = "{base_url}"',
        f'query = "{query}"',
    ]
    if email_env is not None:
        lines.append(f'email_env = "{email_env}"')
    if token_env is not None:
        lines.append(f'token_env = "{token_env}"')
    if extra_tracker:
        lines.append(extra_tracker)
    for name, table in repos.items():
        lines.append("")
        lines.append(f"[repo.{name}]")
        for key, value in table.items():
            lines.append(f"{key} = {value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def configured(env: dict[str, Path], repo: Path) -> dict[str, Path]:
    """A single-profile configuration pointing at the temporary repository."""
    write_config(env["config"], repos={"default": {"path": f'"{repo}"'}})
    return env


# --------------------------------------------------------------------------- #
# Tracker stub
# --------------------------------------------------------------------------- #


def make_ticket(
    key: str = "PROJ-1",
    title: str = "Password reset links expire too early",
    description: str = (
        "Users report that the link in the reset email is already invalid.\n"
        "The value of `RESET_TOKEN_TTL` in src/auth/reset_token.py looks wrong."
    ),
    comments: tuple[Comment, ...] = (),
    attachments: tuple[Attachment, ...] = (),
    priority: str = "High",
    status: str = "Ready for Dev",
) -> Ticket:
    return Ticket(
        key=key,
        title=title,
        description=description,
        url=f"https://example.atlassian.net/browse/{key}",
        status=status,
        priority=priority,
        issue_type="Bug",
        reporter="Reporter Name",
        assignee="Tester",
        labels=("auth",),
        created="2026-01-05T09:00:00.000+0000",
        updated="2026-01-06T11:30:00.000+0000",
        comments=comments,
        attachments=attachments,
        raw={"key": key, "fields": {"summary": title}},
    )


def make_plan(**overrides: object) -> dict[str, object]:
    """A plan that passes every rule, as a base for the failure cases."""
    plan: dict[str, object] = {
        "ticket": "PROJ-1",
        "understanding": "The reset token expires after one hour instead of one day.",
        "changes": [
            {
                "file": "src/auth/reset_token.py",
                "action": "modify",
                "reason": "The TTL constant is wrong.",
                "confidence": "high",
            }
        ],
        "assumptions": ["The TTL is not read from configuration anywhere else."],
        "alternatives_considered": [
            {
                "option": "Make the TTL configurable",
                "rejected_because": "Nobody has asked for it and it widens the change.",
            }
        ],
        "open_questions": [{"text": "Should existing links keep working?", "blocking": False}],
        "acceptance_criteria": ["A link generated now is still accepted 23 hours later."],
        "confidence": "high",
        "needs_human": True,
    }
    plan.update(overrides)
    return plan


def write_plan(worktree: Path, **overrides: object) -> Path:
    """Write a plan into a prepared worktree, the way a design tool would."""
    import json

    path = worktree / ".lazyfish" / "plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(make_plan(**overrides), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


class FakeTracker:
    """In-memory TrackerClient. Records what was asked of it."""

    def __init__(
        self,
        tickets: list[Ticket] | None = None,
        payloads: dict[str, bytes] | None = None,
    ):
        self.tickets = tickets if tickets is not None else [make_ticket()]
        self.payloads = payloads or {}
        self.fetched: list[str] = []
        self.downloaded: list[str] = []
        self.closed = False

    def list_candidates(self, limit: int = 5) -> list[Ticket]:
        return self.tickets[:limit]

    def fetch(self, key: str) -> Ticket:
        self.fetched.append(key)
        for ticket in self.tickets:
            if ticket.key == key:
                return ticket
        raise AssertionError(f"test asked for an unknown ticket: {key}")

    def download_attachment(self, attachment: Attachment) -> bytes:
        self.downloaded.append(attachment.filename)
        return self.payloads.get(attachment.filename, b"stub attachment content")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch) -> FakeTracker:
    """Install a FakeTracker in place of the real client factory."""
    client = FakeTracker()

    def build(_config: object) -> FakeTracker:
        return client

    monkeypatch.setattr("lazyfish.cli.build_client", build)
    return client


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()
