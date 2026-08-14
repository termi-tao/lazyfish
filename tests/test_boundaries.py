"""LF-6 authority and materialisation boundaries.

These tests deliberately exercise artifacts as values rather than test-file
paths.  The three layouts in AC1 are fixture inputs only; the boundary under
test is the promoted TestArtifact patch that materialize applies.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from lazyfish.cli import cli
from lazyfish.db import Database
from lazyfish.errors import LazyfishError, StateError

from .conftest import (
    FakeTracker,
    artifact_store,
    commit_everything,
    git,
    make_ticket,
    open_database,
    task_of,
    tasks_of,
)

CALL_SITES = {
    "architect": {
        "role": "architect",
        "produces": {"TechnicalPlan"},
        "consumes": {"Ticket"},
    },
    "tester@write": {
        "role": "tester",
        "produces": {"TestArtifact"},
        "consumes": {"Ticket", "acceptance_criteria"},
    },
    "tester@verify": {
        "role": "tester",
        "produces": {"TestReport"},
        "consumes": {"TestArtifact", "ImplementationPatch"},
    },
    "coder": {
        "role": "coder",
        "produces": {"ImplementationPatch"},
        "consumes": {"TechnicalPlan", "TestArtifact"},
    },
    "reviewer@tests": {
        "role": "reviewer",
        "produces": {"ReviewReport"},
        "consumes": {"acceptance_criteria", "TestArtifact", "TestReport"},
    },
    "reviewer@impl": {
        "role": "reviewer",
        "produces": {"ReviewReport"},
        "consumes": {
            "TechnicalPlan",
            "TestArtifact",
            "TestReport",
            "ImplementationPatch",
            "ReviewReport",
        },
    },
}

ARTIFACT_TYPES = {
    "TechnicalPlan",
    "TestArtifact",
    "TestReport",
    "ImplementationPatch",
    "ReviewReport",
}


def authority_module():
    """Load the LF-6 authority registry only in tests that require it."""
    import lazyfish.authority

    return lazyfish.authority


def materialize(task, call_site: str):
    """Use LF-6's specified materialisation entry point.

    ASSUMPTION: `materialize(task, call_site)` obtains promoted artifacts from
    persistent lineage, as specified by LF-6 D4/D11.  No second artifact list
    is accepted because that would let a caller substitute unpromoted content.
    """
    from lazyfish.workspace import materialize as materialize_workspace

    return materialize_workspace(task, call_site)


def worktree_from(result: Result) -> Path:
    output = result.stdout
    last = output.strip().splitlines()[-1]
    assert last.startswith("cd "), output
    return Path(last[3:])


def make_layout(repo: Path) -> None:
    """Put all three AC1 test layouts into the committed base revision."""
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "tests" / "test_outside.py").write_text("TEST = 'base'\n", encoding="utf-8")
    (repo / "src" / "neighbour_spec.py").write_text("TEST = 'base'\n", encoding="utf-8")
    (repo / "src" / "shared.py").write_text(
        "IMPLEMENTATION = 'base'\nTEST = 'base'\n", encoding="utf-8"
    )
    (repo / "src" / "implementation.py").write_text("IMPLEMENTATION = 'base'\n", encoding="utf-8")
    commit_everything(repo, "add boundary fixture layouts")


def patch_after_change(worktree: Path, relative: str, replacement: str) -> str:
    """Make one applicable unified diff without leaving the edit in place."""
    path = worktree / relative
    original = path.read_text(encoding="utf-8")
    path.write_text(replacement, encoding="utf-8")
    patch = git(worktree, "diff", "--", relative)
    path.write_text(original, encoding="utf-8")
    return patch


def record_promoted_artifact(
    env: dict[str, Path], task, *, call_site: str, type_name: str, content: dict
) -> str:
    """Persist an artifact exactly as materialize must later consume it."""
    from lazyfish.artifacts import Artifact, content_id

    identifier = content_id(content)
    database = open_database(env)
    try:
        artifact = Artifact(
            id=identifier,
            type=type_name,
            produced_by=CALL_SITES[call_site]["role"],
            call_site=call_site,
            task_id=task.id,
            base_commit=task.base_commit,
        )
        database.record_artifact(artifact)
        artifact_store(env).store(artifact, content)
        database.promote_artifact(task.id, identifier, promoted_by="orchestrator")
    finally:
        database.close()
    return identifier


def prepare_materialisation_case(
    runner: CliRunner,
    configured: dict[str, Path],
    repo: Path,
    tracker: FakeTracker,
) -> tuple[dict[str, Path], object, Path]:
    make_layout(repo)
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    worktree = worktree_from(result)
    task = task_of(configured)

    test_patch = "".join(
        (
            patch_after_change(worktree, "tests/test_outside.py", "TEST = 'promoted outside'\n"),
            patch_after_change(worktree, "src/neighbour_spec.py", "TEST = 'promoted neighbour'\n"),
            patch_after_change(
                worktree,
                "src/shared.py",
                "IMPLEMENTATION = 'base'\nTEST = 'promoted shared'\n",
            ),
        )
    )
    implementation_patch = patch_after_change(
        worktree, "src/implementation.py", "IMPLEMENTATION = 'promoted implementation'\n"
    )

    test_id = record_promoted_artifact(
        configured,
        task,
        call_site="tester@write",
        type_name="TestArtifact",
        content={
            "base_commit": task.base_commit,
            "patch": test_patch,
            "coverage": [{"ac_id": "AC1", "test_ids": ["test_outside"]}],
            "test_command": "pytest",
        },
    )
    record_promoted_artifact(
        configured,
        task,
        call_site="coder",
        type_name="ImplementationPatch",
        content={
            "base_commit": task.base_commit,
            "applied_artifacts": [test_id],
            "patch": implementation_patch,
            "stats": {"files": 1, "insertions": 1, "deletions": 1},
        },
    )
    record_promoted_artifact(
        configured,
        task,
        call_site="tester@verify",
        type_name="TestReport",
        content={"outcome": "RED", "tests": [{"id": "test_outside", "status": "failed"}]},
    )

    # Deliberately poison the old workspace after promotion.  A materialized
    # workspace must be independent of every current workspace state.
    for relative in ("tests/test_outside.py", "src/neighbour_spec.py", "src/shared.py"):
        (worktree / relative).write_text("TEST = 'unpromoted mutation'\n", encoding="utf-8")
    (worktree / "src/implementation.py").write_text(
        "IMPLEMENTATION = 'unpromoted mutation'\n", encoding="utf-8"
    )
    return configured, task, worktree


def test_authority_table_has_the_six_specified_call_sites() -> None:
    """AC3: authority is data for six call sites, not four role-shaped rows."""
    authority = authority_module()
    assert set(authority.CALL_SITES) == set(CALL_SITES)
    for call_site, expected in CALL_SITES.items():
        rule = authority.CALL_SITES[call_site]
        assert rule.role == expected["role"]
        assert set(authority.produces_for(call_site)) == expected["produces"]
        assert set(authority.consumes_for(call_site)) == expected["consumes"]


@pytest.mark.parametrize("call_site", sorted(CALL_SITES))
def test_authority_rejects_every_artifact_outside_a_call_sites_produces(
    call_site: str,
) -> None:
    """AC3: test every forbidden combination rather than a representative one."""
    authority = authority_module()
    permitted = CALL_SITES[call_site]["produces"]
    for type_name in ARTIFACT_TYPES - permitted:
        assert authority.may_produce(call_site, type_name) is False


@pytest.mark.parametrize("call_site", ["reviewer@tests", "reviewer@impl"])
def test_reviewer_cannot_promote_an_implementation_patch(tmp_path: Path, call_site: str) -> None:
    """AC2/AC3: database promotion is also guarded against a direct caller."""
    from lazyfish.artifacts import Artifact

    database = Database(tmp_path / "lazyfish.db")
    database.initialise()
    task = database.insert_task(
        ticket_key="PROJ-1",
        ticket_title="Boundary fixture",
        profile="work",
        branch="lazyfish/PROJ-1",
        worktree_path=str(tmp_path / "worktree"),
        artifacts_path=str(tmp_path / "artifacts"),
        was_top_pick=True,
        base_commit="deadbeef",
    )
    artifact = Artifact(
        id=f"{call_site}-implementation",
        type="ImplementationPatch",
        produced_by="reviewer",
        call_site=call_site,
        task_id=task.id,
        base_commit=task.base_commit,
    )
    database.record_artifact(artifact)
    with pytest.raises(LazyfishError, match="not.*authorized|not.*allowed"):
        database.promote_artifact(task.id, artifact.id, promoted_by="orchestrator")


def test_materialize_restores_promoted_test_content_for_all_three_layouts(
    runner: CliRunner,
    configured: dict[str, Path],
    repo: Path,
    tracker: FakeTracker,
) -> None:
    """AC1: test placement does not change the promoted-test boundary."""
    _, task, _ = prepare_materialisation_case(runner, configured, repo, tracker)

    workspace = materialize(task, "tester@verify").path
    assert (workspace / "tests/test_outside.py").read_bytes() == b"TEST = 'promoted outside'\n"
    assert (workspace / "src/neighbour_spec.py").read_bytes() == b"TEST = 'promoted neighbour'\n"
    assert (workspace / "src/shared.py").read_bytes() == (
        b"IMPLEMENTATION = 'base'\nTEST = 'promoted shared'\n"
    )


@pytest.mark.parametrize(
    ("call_site", "has_implementation_patch"),
    [
        ("tester@write", False),
        ("tester@verify", True),
        ("coder", False),
        ("reviewer@tests", False),
        ("reviewer@impl", True),
    ],
)
def test_materialize_exposes_only_the_artifacts_each_call_site_consumes(
    runner: CliRunner,
    configured: dict[str, Path],
    repo: Path,
    tracker: FakeTracker,
    call_site: str,
    has_implementation_patch: bool,
) -> None:
    """AC5: consumes, not a path ban, determines every workspace read surface."""
    _, task, _ = prepare_materialisation_case(runner, configured, repo, tracker)

    workspace = materialize(task, call_site).path
    implementation = (workspace / "src/implementation.py").read_text(encoding="utf-8")
    assert ("promoted implementation" in implementation) is has_implementation_patch
    if "TestArtifact" in CALL_SITES[call_site]["consumes"]:
        assert "promoted outside" in (workspace / "tests/test_outside.py").read_text(
            encoding="utf-8"
        )
    else:
        assert "promoted outside" not in (workspace / "tests/test_outside.py").read_text(
            encoding="utf-8"
        )


def test_materialize_ignores_unpromoted_workspace_mutations(
    runner: CliRunner,
    configured: dict[str, Path],
    repo: Path,
    tracker: FakeTracker,
) -> None:
    """AC2/AC4: downstream content is a function of base plus promotion only."""
    _, task, _ = prepare_materialisation_case(runner, configured, repo, tracker)

    workspace = materialize(task, "reviewer@impl").path
    assert (workspace / "src/implementation.py").read_text(encoding="utf-8") == (
        "IMPLEMENTATION = 'promoted implementation'\n"
    )
    assert "unpromoted mutation" not in (workspace / "tests/test_outside.py").read_text(
        encoding="utf-8"
    )


def test_notes_are_preserved_or_appended_when_a_plan_is_accepted(tmp_path: Path) -> None:
    """AC9: an earlier human rejection remains visible after later acceptance."""
    database = Database(tmp_path / "lazyfish.db")
    database.initialise()
    task = database.insert_task(
        ticket_key="PROJ-1",
        ticket_title="Notes fixture",
        profile="work",
        branch="lazyfish/PROJ-1",
        worktree_path=str(tmp_path / "worktree"),
        artifacts_path=str(tmp_path / "artifacts"),
        was_top_pick=True,
    )
    database.conn.execute(
        "UPDATE tasks SET state = ?, notes = ? WHERE id = ?",
        ("PLAN_PROMOTED", "rejected because the rollback path was absent", task.id),
    )
    database.conn.commit()

    appended = database.mark_accepted(task.id, plan_accepted=True, notes="accepted after revision")
    assert (
        appended.notes == "rejected because the rollback path was absent\naccepted after revision"
    )

    kept_task = database.insert_task(
        ticket_key="PROJ-2",
        ticket_title="Preserved notes fixture",
        profile="work",
        branch="lazyfish/PROJ-2",
        worktree_path=str(tmp_path / "second-worktree"),
        artifacts_path=str(tmp_path / "second-artifacts"),
        was_top_pick=False,
    )
    database.conn.execute(
        "UPDATE tasks SET state = ?, notes = ? WHERE id = ?",
        ("PLAN_PROMOTED", "human rejection survives", kept_task.id),
    )
    database.conn.commit()
    kept = database.mark_accepted(kept_task.id, plan_accepted=True)
    assert kept.notes == "human rejection survives"


def test_same_profile_allows_different_tickets_but_not_a_second_live_copy(tmp_path: Path) -> None:
    """AC11/AC12: the changed structural invariant is keyed by ticket too."""
    database = Database(tmp_path / "lazyfish.db")
    database.initialise()
    first = database.insert_task(
        ticket_key="PROJ-1",
        ticket_title="one",
        profile="work",
        branch="one",
        worktree_path="/tmp/one",
        artifacts_path="/tmp/one/artifacts",
        was_top_pick=True,
    )
    second = database.insert_task(
        ticket_key="PROJ-2",
        ticket_title="two",
        profile="work",
        branch="two",
        worktree_path="/tmp/two",
        artifacts_path="/tmp/two/artifacts",
        was_top_pick=False,
    )
    assert (first.ticket_key, second.ticket_key) == ("PROJ-1", "PROJ-2")
    with pytest.raises(StateError):
        database.insert_task(
            ticket_key="PROJ-1",
            ticket_title="duplicate",
            profile="work",
            branch="duplicate",
            worktree_path="/tmp/duplicate",
            artifacts_path="/tmp/duplicate/artifacts",
            was_top_pick=False,
        )


def test_the_database_rejects_a_duplicate_live_ticket_inserted_with_raw_sql(tmp_path: Path) -> None:
    """AC12: test the partial unique index, not an application pre-check."""
    database = Database(tmp_path / "lazyfish.db")
    database.initialise()
    database.conn.execute(
        """INSERT INTO tasks (ticket_key, ticket_title, profile, state, branch, worktree_path,
           artifacts_path, was_top_pick, prepared_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "PROJ-1",
            "first",
            "work",
            "PLAN_PROMOTED",
            "one",
            "/tmp/one",
            "/tmp/a",
            1,
            "2026-01-01T00:00:00+00:00",
        ),
    )
    database.conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        database.conn.execute(
            """INSERT INTO tasks (ticket_key, ticket_title, profile, state, branch, worktree_path,
               artifacts_path, was_top_pick, prepared_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "PROJ-1",
                "duplicate",
                "work",
                "READY_FOR_PLAN",
                "two",
                "/tmp/two",
                "/tmp/b",
                1,
                "2026-01-01T00:00:00+00:00",
            ),
        )


def test_old_wip_index_migrates_without_losing_rows_and_is_idempotent(tmp_path: Path) -> None:
    """AC8/AC13: DROP plus CREATE changes the legacy index safely and repeatedly."""
    path = tmp_path / "lazyfish.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, ticket_key TEXT NOT NULL, ticket_title TEXT NOT NULL,
                profile TEXT NOT NULL, state TEXT NOT NULL, branch TEXT NOT NULL,
                worktree_path TEXT NOT NULL, artifacts_path TEXT NOT NULL,
                was_top_pick INTEGER NOT NULL,
                plan_accepted INTEGER, notes TEXT, prepared_at TEXT NOT NULL, accepted_at TEXT,
                abandoned_at TEXT);
            CREATE UNIQUE INDEX ux_tasks_in_flight_profile ON tasks (profile)
                WHERE state = 'READY_FOR_PLAN';"""
        )
        connection.execute(
            """INSERT INTO tasks VALUES (1, 'OLD-1', 'legacy', 'work', 'ABANDONED', 'old',
               '/tmp/old', '/tmp/old/artifacts', 1, NULL, 'keep this', '2026-01-01T00:00:00+00:00',
               NULL, '2026-01-02T00:00:00+00:00')"""
        )
        connection.commit()
    finally:
        connection.close()

    for _ in range(2):
        database = Database(path)
        database.initialise()
        database.close()

    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT ticket_key, notes FROM tasks").fetchone()
        index_sql = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            )
        ]
    finally:
        connection.close()
    assert row == ("OLD-1", "keep this")
    assert any("profile, ticket_key" in sql and "PLAN_PROMOTED" in sql for sql in index_sql)
    assert not any("ON tasks (profile)" in sql for sql in index_sql)


def test_three_bare_preps_keep_one_architect_workspace_and_identical_stdout(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC6/AC15: the new stage directory does not weaken prep idempotence."""
    first = runner.invoke(cli, ["prep"])
    second = runner.invoke(cli, ["prep"])
    third = runner.invoke(cli, ["prep"])

    assert first.exit_code == second.exit_code == third.exit_code == 0
    assert first.stdout == second.stdout == third.stdout
    assert len(tasks_of(configured)) == 1
    assert worktree_from(first).name == "architect"


def test_an_explicit_second_ticket_gets_its_own_workspace_and_branch(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC11: a second ticket is explicit and independent, rather than refused."""
    first = runner.invoke(cli, ["prep", "--ticket", "PROJ-1"])
    assert first.exit_code == 0, first.stdout + first.stderr
    tracker.tickets = [make_ticket("PROJ-2")]
    second = runner.invoke(cli, ["prep", "--ticket", "PROJ-2"])
    assert second.exit_code == 0, second.stdout + second.stderr

    tasks = tasks_of(configured)
    assert [task.ticket_key for task in tasks] == ["PROJ-1", "PROJ-2"]
    assert len({task.worktree_path for task in tasks}) == 2
    assert len({task.branch for task in tasks}) == 2


@pytest.mark.parametrize("command", [["show"], ["promote"], ["accept", "--as-is"], ["next"]])
def test_ambiguous_commands_name_the_candidates_and_require_a_ticket(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker, command: list[str]
) -> None:
    """AC14: no command guesses after two tickets are deliberately opened."""
    assert runner.invoke(cli, ["prep", "--ticket", "PROJ-1"]).exit_code == 0
    tracker.tickets = [make_ticket("PROJ-2")]
    assert runner.invoke(cli, ["prep", "--ticket", "PROJ-2"]).exit_code == 0
    assert len(tasks_of(configured)) == 2

    result = runner.invoke(cli, command)
    assert result.exit_code != 0
    assert "PROJ-1" in result.stdout + result.stderr
    assert "PROJ-2" in result.stdout + result.stderr
    assert "--ticket" in result.stdout + result.stderr


def test_ticket_option_selects_the_requested_task_after_ambiguity(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC14: explicit selection acts on the requested ticket, not the newest row."""
    assert runner.invoke(cli, ["prep", "--ticket", "PROJ-1"]).exit_code == 0
    tracker.tickets = [make_ticket("PROJ-2")]
    assert runner.invoke(cli, ["prep", "--ticket", "PROJ-2"]).exit_code == 0

    result = runner.invoke(cli, ["next", "--ticket", "PROJ-1"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "PROJ-1" in result.stdout


def test_abandon_only_removes_the_selected_tickets_workspace_and_branch(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC16: per-stage cleanup is scoped to one ticket in a multi-ticket profile."""
    # ASSUMPTION: AC16 requires an unambiguous ticket selector for abandon once
    # D8 permits concurrent tickets.  The established CLI spelling is --ticket.
    assert runner.invoke(cli, ["prep", "--ticket", "PROJ-1"]).exit_code == 0
    tracker.tickets = [make_ticket("PROJ-2")]
    assert runner.invoke(cli, ["prep", "--ticket", "PROJ-2"]).exit_code == 0
    before = {task.ticket_key: task for task in tasks_of(configured)}

    result = runner.invoke(cli, ["abandon", "--ticket", "PROJ-1", "--yes"])
    assert result.exit_code == 0, result.stdout + result.stderr
    after = {task.ticket_key: task for task in tasks_of(configured)}
    assert after["PROJ-1"].state == "ABANDONED"
    assert after["PROJ-2"].state != "ABANDONED"
    assert not Path(before["PROJ-1"].worktree_path).exists()
    assert Path(before["PROJ-2"].worktree_path).exists()
