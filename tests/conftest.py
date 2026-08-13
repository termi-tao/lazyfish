"""Shared fixtures.

The suite uses real git repositories and real worktrees in temporary
directories, because worktree behaviour is exactly the kind of thing a mock
would get wrong. Only the tracker is stubbed.

Nothing here touches the developer's own config file or database: every test
redirects both through LAZYFISH_CONFIG and LAZYFISH_DATA_DIR.
"""

from __future__ import annotations

import json
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
    """Redirect config and state into the temporary directory.

    Credentials are deliberately NOT put in the environment here: the file is
    the normal path and the one worth exercising. Tests that want the override
    set the two variables themselves.
    """
    config_file = tmp_path / "config" / "config.toml"
    credentials_file = tmp_path / "config" / "credentials"
    data_dir = tmp_path / "data"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LAZYFISH_CONFIG", str(config_file))
    monkeypatch.setenv("LAZYFISH_DATA_DIR", str(data_dir))
    for name in ("LAZYFISH_EMAIL", "LAZYFISH_TOKEN", "LAZYFISH_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    return {"config": config_file, "credentials": credentials_file, "data": data_dir}


def toml_value(value: object) -> str:
    """Render a Python value as TOML, for building fixture files by hand."""
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, Path):
        return json.dumps(str(value))
    return str(value)


def write_config(
    path: Path,
    *,
    profiles: dict[str, dict[str, object]],
    defaults: dict[str, object] | None = None,
    default_profile: str | None = "work",
) -> Path:
    """Write a config.toml. `profiles` maps profile name to its keys.

    Identity keys missing from a profile are filled in with working values, so a
    test that cares about one key does not have to spell out the other three.
    """
    lines: list[str] = []
    if default_profile is not None:
        lines.append(f"default_profile = {toml_value(default_profile)}")
    if defaults:
        lines.append("")
        lines.append("[defaults]")
        lines.extend(f"{key} = {toml_value(value)}" for key, value in defaults.items())
    for name, table in profiles.items():
        lines.append("")
        lines.append(f"[profile.{name}]")
        filled = {
            "tracker": "jira-cloud",
            "base_url": "https://example.atlassian.net",
            "query": f"project = {name.upper()} AND assignee = currentUser()",
            **table,
        }
        lines.extend(f"{key} = {toml_value(value)}" for key, value in filled.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_credentials(
    path: Path,
    *,
    sections: dict[str, dict[str, str]] | None = None,
    mode: int = 0o600,
) -> Path:
    """Write a credentials file and set its mode (600 unless a test says otherwise)."""
    sections = sections or {"work": {"email": "you@example.com", "api_token": "token-work"}}
    lines: list[str] = []
    for name, table in sections.items():
        lines.append(f"[{name}]")
        lines.extend(f"{key} = {toml_value(value)}" for key, value in table.items())
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture
def configured(env: dict[str, Path], repo: Path) -> dict[str, Path]:
    """One profile named 'work', with credentials on disk at mode 600."""
    write_config(env["config"], profiles={"work": {"repo": str(repo)}})
    write_credentials(env["credentials"])
    return env


@pytest.fixture
def two_profiles(env: dict[str, Path], repo: Path, second_repo: Path) -> dict[str, Path]:
    """Two profiles with different queries and different repositories (AC1)."""
    write_config(
        env["config"],
        profiles={
            "work": {"repo": str(repo), "query": "project = WORK"},
            "infra": {"repo": str(second_repo), "query": "project = INFRA"},
        },
    )
    write_credentials(
        env["credentials"],
        sections={
            "work": {"email": "you@example.com", "api_token": "token-work"},
            "infra": {"email": "you@example.com", "api_token": "token-infra"},
        },
    )
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
    """In-memory TrackerClient. Records what was asked of it.

    Candidates can be keyed by query, which is what lets a test prove that two
    profiles really do reach the tracker with different queries (AC1) rather
    than merely being named differently.
    """

    def __init__(
        self,
        tickets: list[Ticket] | None = None,
        payloads: dict[str, bytes] | None = None,
    ):
        self.tickets = tickets if tickets is not None else [make_ticket()]
        self.by_query: dict[str, list[Ticket]] = {}
        self.payloads = payloads or {}
        self.built: list[tuple[str, str, str]] = []
        self.active_query = ""
        self.fetched: list[str] = []
        self.downloaded: list[str] = []
        self.closed = False

    def candidates_for(self, query: str) -> list[Ticket]:
        return self.by_query.get(query, self.tickets)

    def list_candidates(self, limit: int = 5) -> list[Ticket]:
        return self.candidates_for(self.active_query)[:limit]

    def fetch(self, key: str) -> Ticket:
        self.fetched.append(key)
        for ticket in (*self.candidates_for(self.active_query), *self.tickets):
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
    """Install a FakeTracker in place of the real client factory.

    The stub stands in for build_client, so it sees exactly what the real
    factory would: the resolved profile and the resolved credentials.
    """
    client = FakeTracker()

    def build(profile: object, credentials: object) -> FakeTracker:
        client.built.append(
            (profile.name, profile.query, credentials.api_token)  # type: ignore[attr-defined]
        )
        client.active_query = profile.query  # type: ignore[attr-defined]
        return client

    monkeypatch.setattr("lazyfish.cli.build_client", build)
    return client


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()
