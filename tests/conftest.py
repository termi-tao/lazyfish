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

from lazyfish.cli import cli
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


def head_commit(repo: Path) -> str:
    """The commit a repository (or worktree) currently has checked out."""
    return git(repo, "rev-parse", "HEAD").strip()


def commit_everything(repo: Path, message: str = "work in progress") -> str:
    """Stage and commit whatever is in a working tree; returns the new HEAD.

    Used to move HEAD forward under a task whose baseline was recorded earlier,
    which is how LF-5 AC4 distinguishes "reads the database" from "reads git".
    """
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return head_commit(repo)


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
    """One profile named 'work', with credentials on disk at mode 600.

    `test_command` is a shell no-op: the verification stage runs whatever the
    profile configures, and a suite that always passes keeps tests that are not
    about verification from depending on one.
    """
    write_config(
        env["config"],
        profiles={"work": {"repo": str(repo), "test_command": "sh -c 'exit 0'"}},
    )
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


# --------------------------------------------------------------------------- #
# Reading back what a command wrote
# --------------------------------------------------------------------------- #
#
# The imports inside these helpers are deliberate: lazyfish.artifacts does not
# exist until LF-5 is implemented, and a module-level import of it here would
# stop the whole suite from being collected rather than failing the tests that
# actually depend on it.


def open_database(env: dict[str, Path]):
    """The database a command under test wrote to."""
    from lazyfish.db import Database

    database = Database(env["data"] / "lazyfish.db")
    database.initialise()
    return database


def tasks_of(env: dict[str, Path]) -> list:
    database = open_database(env)
    try:
        return database.list_tasks()
    finally:
        database.close()


def task_of(env: dict[str, Path], index: int = 0):
    return tasks_of(env)[index]


def artifacts_of(env: dict[str, Path], task_id: int) -> list:
    """Artifact metadata rows for one task, oldest first."""
    database = open_database(env)
    try:
        return database.list_artifacts(task_id)
    finally:
        database.close()


def artifact_store(env: dict[str, Path]):
    from lazyfish.artifacts import ArtifactStore

    return ArtifactStore(env["data"] / "artifacts")


def stored_content(env: dict[str, Path], task_id: int, artifact_id: str) -> dict:
    """The bytes an artifact was promoted with, parsed back."""
    return artifact_store(env).load(task_id, artifact_id)


def stored_artifact_files(env: dict[str, Path], task_id: int) -> list[Path]:
    directory = env["data"] / "artifacts" / str(task_id)
    if not directory.exists():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file())


# --------------------------------------------------------------------------- #
# An agent that did far more than it was asked to
# --------------------------------------------------------------------------- #

IMPLEMENTATION_FILE_COUNT = 20


MODIFIED_TRACKED_FILES = ("src/auth/reset_token.py", "README.md")


def implement_the_whole_ticket(worktree: Path) -> list[str]:
    """Write a full implementation into a workspace, as K0 describes.

    This is the situation LF-5 exists for: the design stage also implemented the
    ticket. None of it may survive promotion, and none of it may make promotion
    fail either (AC1).

    It rewrites the two tracked files named in MODIFIED_TRACKED_FILES and
    creates a pile of new ones. Only the new paths are returned, because those
    are the ones no downstream output may mention - the plan itself legitimately
    names src/auth/reset_token.py in its `changes`.
    """
    (worktree / "src" / "auth" / "reset_token.py").write_text(
        "RESET_TOKEN_TTL = 86400\n\n\ndef build_reset_link(user_id):\n"
        "    return f'/reset/{user_id}?v=2'\n",
        encoding="utf-8",
    )
    (worktree / "README.md").write_text(
        "# target repo\n\nNow with a rewritten reset flow.\n", encoding="utf-8"
    )

    created: list[str] = []
    for index in range(IMPLEMENTATION_FILE_COUNT):
        relative = f"src/auth/generated_module_{index:02d}.py"
        (worktree / relative).write_text(
            f"MARKER_{index:02d} = 'written by the design stage'\n", encoding="utf-8"
        )
        created.append(relative)

    relative = "tests/test_generated_reset_token.py"
    # The directory has to be created: the repo fixture makes an empty tests/,
    # and git does not track empty directories, so a worktree checkout has no
    # tests/ in it. An agent writing a new test file would create the directory
    # too, so this is what the situation being reproduced actually looks like.
    (worktree / relative).parent.mkdir(parents=True, exist_ok=True)
    (worktree / relative).write_text("def test_generated():\n    assert True\n", encoding="utf-8")
    created.append(relative)

    return created


@pytest.fixture
def prepared_worktree(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> tuple[dict[str, Path], Path, FakeTracker]:
    """A worktree prepared for PROJ-1, waiting for a plan.

    Same shape as the `prepared` fixture in test_flow.py, hoisted here so the
    promotion tests can use it without importing from another test module.
    """
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    last = result.stdout.strip().splitlines()[-1]
    assert last.startswith("cd "), last
    return configured, Path(last[3:]), tracker


# --------------------------------------------------------------------------- #
# A database written by the previous release
# --------------------------------------------------------------------------- #

LEGACY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key     TEXT    NOT NULL,
    ticket_title   TEXT    NOT NULL,
    profile   TEXT    NOT NULL,
    state          TEXT    NOT NULL,
    branch         TEXT    NOT NULL,
    worktree_path  TEXT    NOT NULL,
    artifacts_path TEXT    NOT NULL,
    was_top_pick   INTEGER NOT NULL,
    plan_accepted  INTEGER,
    notes          TEXT,
    prepared_at    TEXT    NOT NULL,
    accepted_at    TEXT,
    abandoned_at   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_in_flight_profile
    ON tasks (profile) WHERE state = 'READY_FOR_PLAN';  -- retired-vocabulary: on purpose

CREATE INDEX IF NOT EXISTS ix_tasks_ticket ON tasks (ticket_key, profile);
"""
"""The schema as of 4c1a7aa, frozen.

A copy rather than an import on purpose: this is the shape of the database
already sitting on the user's disk, so it must not follow db.py forward when
db.py changes. Migrating it is LF-5 AC7.
"""

# Mirrors the five rows in the user's own database: four abandoned tool-testing
# tickets and one still awaiting a plan. They are real data and must survive
# (R2).
LEGACY_ROWS = (
    ("CS-100", "First tool test", "spendwatt", "ABANDONED"),
    ("CS-233", "Second tool test", "spendwatt", "ABANDONED"),
    ("CS-291", "Third tool test", "spendwatt", "ABANDONED"),
    ("CS-344", "Fourth tool test", "spendwatt", "ABANDONED"),
    ("CS-370", "The ticket still in flight", "spendwatt", "AWAITING_ARTIFACT"),
)


def write_legacy_database(path: Path) -> Path:
    """Create a database in the pre-LF-5 shape, carrying the five real rows."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(LEGACY_SCHEMA_SQL)
        for index, (key, title, profile, state) in enumerate(LEGACY_ROWS):
            connection.execute(
                """
                INSERT INTO tasks (
                    ticket_key, ticket_title, profile, state, branch,
                    worktree_path, artifacts_path, was_top_pick, plan_accepted,
                    notes, prepared_at, accepted_at, abandoned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    title,
                    profile,
                    state,
                    f"lazyfish/{key}",
                    f"/home/user/.local/share/lazyfish/worktrees/{profile}/{key}",
                    f"/home/user/.local/share/lazyfish/worktrees/{profile}/{key}/artifacts/{key}",
                    1,
                    None,
                    f"note {index}" if index else None,
                    "2026-08-01T09:00:00+00:00",
                    None,
                    "2026-08-01T10:00:00+00:00" if state == "ABANDONED" else None,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def legacy_rows_of(path: Path) -> list[tuple]:
    """Every legacy column of every row, for a before/after comparison."""
    import sqlite3

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return [
            tuple(row[column] for column in LEGACY_COLUMNS)
            for row in connection.execute("SELECT * FROM tasks ORDER BY id")
        ]
    finally:
        connection.close()


LEGACY_COLUMNS = (
    "id",
    "ticket_key",
    "ticket_title",
    "profile",
    "state",
    "branch",
    "worktree_path",
    "artifacts_path",
    "was_top_pick",
    "plan_accepted",
    "notes",
    "prepared_at",
    "accepted_at",
    "abandoned_at",
)


# --------------------------------------------------------------------------- #
# The tester stage (LF-8)
# --------------------------------------------------------------------------- #


def write_tests(
    workspace: Path,
    *,
    path: str = "tests/test_generated.py",
    body: str = "def test_generated():\n    assert True\n",
    coverage: list[dict[str, object]] | None = None,
    criteria: int = 1,
) -> Path:
    """Write a test file and the coverage table that declares it.

    Defaults to covering every criterion of `make_plan`'s plan with the one file
    it writes, which is what a test that is not about coverage wants.
    """
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")

    if coverage is None:
        coverage = [
            {"ac_id": f"AC{index + 1}", "test_ids": [f"{path}::test_generated"]}
            for index in range(criteria)
        ]
    state = workspace / ".lazyfish"
    state.mkdir(parents=True, exist_ok=True)
    (state / "tests-coverage.json").write_text(
        json.dumps({"coverage": coverage}, indent=2), encoding="utf-8"
    )
    return target


def stage_workspace(env: dict[str, Path], call_site: str, profile: str = "work") -> Path:
    """Where one call site works, for the one live ticket of `profile`."""
    with open_database(env) as database:
        task = database.get_live(profile)[0]
    return Path(task.worktree_path).parent / call_site


def workspace_for_tests(env: dict[str, Path], profile: str = "work") -> Path:
    """Where the tests stage works, for the one live ticket of `profile`.

    Not named `tester_workspace`: pytest collects `test*`, and a helper whose
    name starts with those four letters is picked up as a test case.
    """
    return stage_workspace(env, "tester@write", profile)


def write_implementation(
    workspace: Path, path: str = "src/auth/reset_token.py", body: str = "RESET_TOKEN_TTL = 86400\n"
) -> Path:
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def write_report(workspace: Path, outcome: str = "GREEN", tests: list | None = None) -> Path:
    state = workspace / ".lazyfish"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "test-report.json"
    path.write_text(
        json.dumps({"outcome": outcome, "tests": tests or []}, indent=2), encoding="utf-8"
    )
    return path


def promote(runner, profile: str = "work"):
    from lazyfish.cli import cli

    arguments = ["promote"] if profile == "work" else ["--profile", profile, "promote"]
    result = runner.invoke(cli, arguments)
    assert result.exit_code == 0, result.stdout + result.stderr
    return result


def finish_the_tester_stage(
    env: dict[str, Path], runner, *, profile: str = "work", criteria: int = 1
):
    """Take the ticket through every stage after the plan, so that it completes.

    A ticket no longer ends at `accept`. Tests that are about the first stage
    use this to reach the end without restating the three that follow.
    """
    write_tests(workspace_for_tests(env, profile), criteria=criteria)
    promote(runner, profile)

    write_implementation(stage_workspace(env, "coder", profile))
    promote(runner, profile)

    write_report(stage_workspace(env, "tester@verify", profile))
    return promote(runner, profile)
