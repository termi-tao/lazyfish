"""C1: the workspace drift is measured, recorded, and decides nothing.

Written by the implementer, not by a separate session. LF-5's guarantee -- that
whoever wrote the tests was not whoever wrote the code -- does not hold here, and
the plan says so in as many words. The scope is small and adds no gate, which is
why the weaker guarantee was accepted for this ticket and not for the next one.

The property under test is stated twice, from both sides:

    the number is right                     AC2, AC3, AC4
    the number changes nothing              AC1, AC6

The second half is the one that will decay. A measurement and a gate are one `if`
apart, so most of this file is about promotion behaving identically whatever the
count says.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from lazyfish.cli import cli
from lazyfish.db import STATE_PLAN_PROMOTED, Database
from lazyfish.workspace import measure_drift

from .conftest import (
    IMPLEMENTATION_FILE_COUNT,
    MODIFIED_TRACKED_FILES,
    FakeTracker,
    commit_everything,
    git,
    implement_the_whole_ticket,
    task_of,
    write_plan,
)

# The 20 generated modules plus the generated test file, plus the two tracked
# files the helper rewrites. What lazyfish itself writes is not in here, which is
# the whole point of AC3.
EXPECTED_DRIFT_FILES = IMPLEMENTATION_FILE_COUNT + 1 + len(MODIFIED_TRACKED_FILES)

# The part of the notice that carries the point: those changes are ignored, not
# forbidden. Asserted rather than the whole sentence so that rewording it stays
# cheap while removing the meaning does not.
NOTICE = "none of it will be promoted"


def columns_of(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# AC2 and AC3: the number is right, and it is about the agent
# --------------------------------------------------------------------------- #


def test_a_plan_on_its_own_drifts_by_nothing(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC3: what lazyfish writes into the workspace is not the agent's doing.

    A freshly prepared worktree already contains `.lazyfish/` and `CLAUDE.md`.
    Counting those would give every ticket the same constant offset, and the
    measurement would be describing the tool rather than the stage.
    """
    env, worktree, _ = prepared_worktree
    write_plan(worktree)

    assert runner.invoke(cli, ["promote"]).exit_code == 0

    task = task_of(env)
    assert task.workspace_delta_files == 0
    assert task.workspace_delta_lines == 0


def test_an_implemented_ticket_is_counted(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC2: the case K0 was about, now with a number on it."""
    env, worktree, _ = prepared_worktree
    implement_the_whole_ticket(worktree)
    write_plan(worktree)

    assert runner.invoke(cli, ["promote"]).exit_code == 0

    task = task_of(env)
    assert task.workspace_delta_files == EXPECTED_DRIFT_FILES
    assert task.workspace_delta_lines is not None and task.workspace_delta_lines > 0


def test_new_files_and_edited_files_both_count(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """One index pass has to see both, or the count is only half the story."""
    env, worktree, _ = prepared_worktree
    (worktree / "src" / "auth" / "brand_new.py").write_text("X = 1\n", encoding="utf-8")
    (worktree / "README.md").write_text("# rewritten\n", encoding="utf-8")
    write_plan(worktree)

    assert runner.invoke(cli, ["promote"]).exit_code == 0
    assert task_of(env).workspace_delta_files == 2


def test_the_count_is_reported_to_the_person(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC8: the number is useless if it only ever reaches the database."""
    env, worktree, _ = prepared_worktree
    implement_the_whole_ticket(worktree)
    write_plan(worktree)

    result = runner.invoke(cli, ["promote"])
    combined = result.stdout + result.stderr
    assert str(EXPECTED_DRIFT_FILES) in combined
    assert NOTICE in combined


def test_nothing_is_said_when_there_is_nothing_to_say(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """A clean workspace should not be told about its own cleanliness."""
    _, worktree, _ = prepared_worktree
    write_plan(worktree)

    result = runner.invoke(cli, ["promote"])
    assert NOTICE not in result.stdout + result.stderr


def test_status_reports_the_average(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC8: a trend a person can watch without querying the database."""
    env, worktree, _ = prepared_worktree
    implement_the_whole_ticket(worktree)
    write_plan(worktree)
    assert runner.invoke(cli, ["accept", "--as-is"]).exit_code == 0

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "workspace drift" in result.stdout
    assert str(EXPECTED_DRIFT_FILES) in result.stdout


def test_status_says_n_a_before_anything_was_measured(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """No measurements and all-zero measurements are different facts."""
    assert runner.invoke(cli, ["prep"]).exit_code == 0
    result = runner.invoke(cli, ["status"])
    assert "workspace drift" in result.stdout
    line = next(row for row in result.stdout.splitlines() if "workspace drift" in row)
    assert "n/a" in line


# --------------------------------------------------------------------------- #
# AC4 and AC5: the baseline, and the repository's own state
# --------------------------------------------------------------------------- #


def test_the_baseline_is_the_recorded_one_not_head(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC4: committing inside the workspace does not make the drift disappear.

    The same rule promotion follows for its own baseline. If the comparison used
    HEAD, an agent could erase the measurement with one commit -- and `git commit`
    is well within what an agent does on its own.
    """
    env, worktree, _ = prepared_worktree
    implement_the_whole_ticket(worktree)
    write_plan(worktree)
    commit_everything(worktree, "the design stage committed its work")
    assert not git(worktree, "status", "--porcelain").strip()

    assert runner.invoke(cli, ["promote"]).exit_code == 0

    task = task_of(env)
    assert task.workspace_delta_files == EXPECTED_DRIFT_FILES


def test_the_repositorys_own_index_is_not_touched(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC5: measuring stages everything, but into an index of its own.

    Staging into the real index would leave the user's worktree in a state they
    did not ask for, and would be a second thing promotion silently does to the
    repository it is supposed to only read.
    """
    env, worktree, _ = prepared_worktree
    implement_the_whole_ticket(worktree)
    write_plan(worktree)
    before = git(worktree, "status", "--porcelain")

    assert runner.invoke(cli, ["promote"]).exit_code == 0
    assert git(worktree, "status", "--porcelain") == before


def test_measuring_twice_gives_the_same_answer(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """Determinism, and evidence the first call left nothing behind."""
    env, worktree, _ = prepared_worktree
    implement_the_whole_ticket(worktree)
    baseline = task_of(env).base_commit
    assert baseline is not None

    first = measure_drift(worktree, baseline)
    second = measure_drift(worktree, baseline)
    assert first == second
    assert first is not None and first.files == EXPECTED_DRIFT_FILES


# --------------------------------------------------------------------------- #
# AC1 and AC6: the measurement decides nothing
# --------------------------------------------------------------------------- #


def test_a_large_drift_still_promotes(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC1: this is the line C1 must not cross.

    LF-5's whole argument is that workspace changes are *ignored* rather than
    refused, because ignoring needs no knowledge of what a test file is. Counting
    them must not quietly turn that back into refusing.
    """
    env, worktree, _ = prepared_worktree
    implement_the_whole_ticket(worktree)
    write_plan(worktree)

    result = runner.invoke(cli, ["promote"])
    assert result.exit_code == 0, result.stdout + result.stderr

    task = task_of(env)
    assert task.state == STATE_PLAN_PROMOTED
    assert task.workspace_delta_files == EXPECTED_DRIFT_FILES


def test_the_promoted_artifact_is_unaffected_by_the_drift(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """The artifact id is the content of the plan, and drift is not in it.

    If the count reached the artifact, the same plan produced in two workspaces
    would get two ids, and LF-5 AC10's idempotence would break.
    """
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["promote", "--json"]).exit_code == 0
    clean = json.loads(runner.invoke(cli, ["promote", "--json"]).stdout)["artifact"]["id"]

    env2, worktree2, _ = prepared_worktree
    implement_the_whole_ticket(worktree2)
    assert runner.invoke(cli, ["promote", "--json"]).exit_code == 0
    dirty = json.loads(runner.invoke(cli, ["promote", "--json"]).stdout)["artifact"]["id"]

    assert clean == dirty


def test_an_unmeasurable_workspace_still_promotes(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC6: a failed measurement is a warning, never a refusal.

    The baseline is forced to a commit that does not exist, which is what a
    corrupted row or a rewritten history looks like from here.
    """
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    database = Database(env["data"] / "lazyfish.db")
    try:
        database.conn.execute("UPDATE tasks SET base_commit = ?", ("f" * 40,))
        database.conn.commit()
    finally:
        database.close()

    result = runner.invoke(cli, ["promote"])
    assert result.exit_code == 0, result.stdout + result.stderr

    task = task_of(env)
    assert task.state == STATE_PLAN_PROMOTED
    assert task.workspace_delta_files is None
    assert task.workspace_delta_lines is None
    assert "unknown" in (result.stdout + result.stderr).lower()


def test_a_rejected_plan_is_still_measured(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """A stage that failed its contract is exactly one worth having a number for."""
    env, worktree, _ = prepared_worktree
    implement_the_whole_ticket(worktree)
    write_plan(worktree, needs_human=False)

    assert runner.invoke(cli, ["promote"]).exit_code == 6
    assert task_of(env).workspace_delta_files == EXPECTED_DRIFT_FILES


def test_the_orchestrator_never_sees_the_drift() -> None:
    """R1, as a criterion rather than an intention.

    The decision layer must not be able to read the measurement, because the
    cheapest possible mistake here is one `if` in `decide_promotion`.
    """
    from pathlib import Path as _Path

    import lazyfish.orchestrator as orchestrator

    source = _Path(orchestrator.__file__).read_text(encoding="utf-8")
    for name in ("drift", "workspace_delta", "measure_drift"):
        assert name not in source, name


# --------------------------------------------------------------------------- #
# AC7: the migration, on the template LF-5 established
# --------------------------------------------------------------------------- #

LEGACY_LF5_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key     TEXT    NOT NULL,
    ticket_title   TEXT    NOT NULL,
    profile        TEXT    NOT NULL,
    state          TEXT    NOT NULL,
    branch         TEXT    NOT NULL,
    worktree_path  TEXT    NOT NULL,
    artifacts_path TEXT    NOT NULL,
    was_top_pick   INTEGER NOT NULL,
    plan_accepted  INTEGER,
    notes          TEXT,
    prepared_at    TEXT    NOT NULL,
    accepted_at    TEXT,
    abandoned_at   TEXT,
    base_commit    TEXT,
    attempt          INTEGER NOT NULL DEFAULT 0,
    ticket_attempts  INTEGER NOT NULL DEFAULT 0,
    escalation_reason TEXT,
    approved_via      TEXT
);
"""


def write_lf5_database(path: Path) -> Path:
    """A database in the shape LF-5 left behind: everything except C1's columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(LEGACY_LF5_SQL)
        connection.execute(
            """
            INSERT INTO tasks (
                ticket_key, ticket_title, profile, state, branch, worktree_path,
                artifacts_path, was_top_pick, prepared_at
            ) VALUES ('CS-1', 'a real row', 'spendwatt', 'PLAN_APPROVED',
                      'lazyfish/CS-1', '/w', '/w/a', 1, '2026-08-01T09:00:00+00:00')
            """
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_a_database_from_lf5_gains_the_two_columns(tmp_path: Path) -> None:
    """AC7, third shape. The template is meant to be reused, so it is exercised."""
    path = write_lf5_database(tmp_path / "lazyfish.db")
    assert "workspace_delta_files" not in columns_of(path)

    Database(path).initialise()
    assert {"workspace_delta_files", "workspace_delta_lines"} <= columns_of(path)


def test_the_existing_row_survives_and_reads_back(tmp_path: Path) -> None:
    """Additive only: no row is rewritten, and the new columns read as unmeasured."""
    path = write_lf5_database(tmp_path / "lazyfish.db")
    database = Database(path)
    database.initialise()
    try:
        tasks = database.list_tasks()
        assert [task.ticket_key for task in tasks] == ["CS-1"]
        assert tasks[0].workspace_delta_files is None
        assert tasks[0].workspace_delta_lines is None
        assert tasks[0].attempt == 0
    finally:
        database.close()


def test_migrating_twice_changes_nothing(tmp_path: Path) -> None:
    """AC7, second shape."""
    path = write_lf5_database(tmp_path / "lazyfish.db")
    Database(path).initialise()
    columns = columns_of(path)
    for _ in range(2):
        instance = Database(path)
        instance.initialise()
        instance.close()
    assert columns_of(path) == columns


def test_a_new_database_has_them_from_the_start(tmp_path: Path) -> None:
    """AC7, first shape."""
    path = tmp_path / "lazyfish.db"
    Database(path).initialise()
    assert {"workspace_delta_files", "workspace_delta_lines"} <= columns_of(path)


@pytest.mark.parametrize("value", [0, 23])
def test_a_measurement_of_zero_is_not_the_same_as_no_measurement(
    tmp_path: Path, value: int
) -> None:
    """The distinction the average depends on: absent is not zero."""
    database = Database(tmp_path / "lazyfish.db")
    database.initialise()
    try:
        task = database.insert_task(
            ticket_key="PROJ-1",
            ticket_title="t",
            profile="work",
            branch="b",
            worktree_path="/w",
            artifacts_path="/w/a",
            was_top_pick=True,
        )
        assert database.stats("work")[0].average_delta_files is None
        database.record_drift(task.id, value, value * 10)
        assert database.stats("work")[0].average_delta_files == value
    finally:
        database.close()
