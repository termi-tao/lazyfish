"""State storage: legal transitions, one task in flight, statistics, migration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lazyfish import db as db_module
from lazyfish.db import (
    STATE_ABANDONED,
    STATE_PLAN_APPROVED,
    STATE_READY_FOR_PLAN,
    Database,
    check_transition,
)
from lazyfish.errors import StateError

from .conftest import LEGACY_ROWS, legacy_rows_of, write_legacy_database

# The three states LF-5 adds. Spelled out rather than imported so that this
# file - which carries the pre-existing db tests - still collects while the
# slice is being written; test_the_new_states_are_exported below is what checks
# that db.py names them, and the other new test modules import them directly.
STATE_PLAN_PROMOTED = "PLAN_PROMOTED"
STATE_REJECTED = "REJECTED"
STATE_ESCALATED = "ESCALATED"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "lazyfish.db")
    instance.initialise()
    return instance


UNSET = object()


def add(
    database: Database,
    key: str = "PROJ-1",
    profile: str = "work",
    top: bool = True,
    base_commit: str | None | object = UNSET,
):
    """Insert a task.

    base_commit is forwarded only when a case asks for it: the cases that
    predate LF-5 must keep calling the signature they were written against, so
    that they stay a regression net for the migration rather than turning red
    on it.
    """
    baseline = {} if base_commit is UNSET else {"base_commit": base_commit}
    return database.insert_task(
        ticket_key=key,
        ticket_title=f"title for {key}",
        profile=profile,
        branch=f"lazyfish/{key}",
        worktree_path=f"/tmp/worktrees/{profile}/{key}",
        artifacts_path=f"/tmp/worktrees/{profile}/{key}/artifacts/{key}",
        was_top_pick=top,
        **baseline,
    )


def force_state(database: Database, task_id: int, state: str):
    """Move a row into a state without replaying the flow that reaches it.

    Used where a test is about what happens *from* a state, not about how the
    state was arrived at. Promotion itself is covered in test_promotion.py.
    """
    database.conn.execute("UPDATE tasks SET state = ? WHERE id = ?", (state, task_id))
    database.conn.commit()
    return database.get(task_id)


def columns_of(path: Path, table: str = "tasks") -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in rows}
    finally:
        connection.close()


def test_initialise_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "lazyfish.db"
    for _ in range(3):
        instance = Database(path)
        instance.initialise()
        instance.close()
    assert path.exists()


def test_insert_and_read_back(database: Database) -> None:
    task = add(database)
    assert task.state == STATE_READY_FOR_PLAN
    assert task.was_top_pick is True
    assert task.plan_accepted is None
    assert database.get_in_flight("work").id == task.id
    assert database.get_by_ticket("PROJ-1", "work").id == task.id


def test_one_live_task_per_ticket_not_per_profile(database: Database) -> None:
    """LF-6 D8/D9: profiles may hold many tickets, never two copies of one."""
    first = add(database, "PROJ-1")
    second = add(database, "PROJ-2")
    assert (first.ticket_key, second.ticket_key) == ("PROJ-1", "PROJ-2")
    with pytest.raises(StateError):
        add(database, "PROJ-1")


def test_profiles_do_not_interfere(database: Database) -> None:
    first = add(database, "PROJ-1", profile="work")
    second = add(database, "WEB-9", profile="infra")
    assert database.get_in_flight("work").id == first.id
    assert database.get_in_flight("infra").id == second.id


def test_accept_records_the_outcome(database: Database) -> None:
    task = add(database)
    force_state(database, task.id, STATE_PLAN_PROMOTED)
    accepted = database.mark_accepted(task.id, plan_accepted=False, notes="missed a case")
    assert accepted.state == STATE_PLAN_APPROVED
    assert accepted.plan_accepted is False
    assert accepted.notes == "missed a case"
    assert accepted.accepted_at is not None


def test_abandon_frees_the_profile(database: Database) -> None:
    task = add(database, "PROJ-1")
    database.mark_abandoned(task.id, notes="deprioritised")
    assert database.get_in_flight("work") is None
    next_task = add(database, "PROJ-2")
    assert next_task.ticket_key == "PROJ-2"


def test_the_same_ticket_can_be_prepared_again_after_abandon(
    database: Database,
) -> None:
    first = add(database, "PROJ-1")
    database.mark_abandoned(first.id)
    second = add(database, "PROJ-1")
    assert second.id != first.id


def test_abandoned_is_terminal(database: Database) -> None:
    task = add(database)
    database.mark_abandoned(task.id)
    with pytest.raises(StateError, match="terminal state"):
        database.mark_accepted(task.id, plan_accepted=True)


def test_a_task_cannot_be_accepted_twice(database: Database) -> None:
    task = add(database)
    force_state(database, task.id, STATE_PLAN_PROMOTED)
    database.mark_accepted(task.id, plan_accepted=True)
    with pytest.raises(StateError, match="Cannot move a task"):
        database.mark_accepted(task.id, plan_accepted=True)


# --------------------------------------------------------------------------- #
# LF-5 D7: the state machine, one row per edge
# --------------------------------------------------------------------------- #


def test_the_new_states_are_exported() -> None:
    """Names, not literals: the CLI, the orchestrator and the tests share them."""
    for name, value in (
        ("STATE_PLAN_PROMOTED", STATE_PLAN_PROMOTED),
        ("STATE_REJECTED", STATE_REJECTED),
        ("STATE_ESCALATED", STATE_ESCALATED),
    ):
        assert getattr(db_module, name, None) == value, name


def test_check_transition_table() -> None:
    """The legal edges of the extended machine.

    PLAN_APPROVED keeps its original name and meaning - the state after a person
    approved - and PLAN_PROMOTED is inserted in front of it (Q2, trap 2).
    """
    check_transition(STATE_READY_FOR_PLAN, STATE_PLAN_PROMOTED)
    check_transition(STATE_READY_FOR_PLAN, STATE_REJECTED)
    check_transition(STATE_READY_FOR_PLAN, STATE_ESCALATED)
    check_transition(STATE_REJECTED, STATE_PLAN_PROMOTED)
    check_transition(STATE_REJECTED, STATE_READY_FOR_PLAN)
    check_transition(STATE_REJECTED, STATE_ESCALATED)
    check_transition(STATE_PLAN_PROMOTED, STATE_PLAN_APPROVED)
    check_transition(STATE_PLAN_PROMOTED, STATE_READY_FOR_PLAN)


@pytest.mark.parametrize(
    "state",
    [
        STATE_READY_FOR_PLAN,
        STATE_PLAN_PROMOTED,
        STATE_PLAN_APPROVED,
        STATE_REJECTED,
        STATE_ESCALATED,
    ],
)
def test_abandon_is_reachable_from_every_state(state: str) -> None:
    """D7: ABANDONED is reachable from any state, and R4 depends on it."""
    check_transition(state, STATE_ABANDONED)


def test_a_plan_cannot_be_approved_before_it_is_promoted() -> None:
    """AC13, last bullet. This is the edge LF-1 had and LF-5 removes."""
    with pytest.raises(StateError):
        check_transition(STATE_READY_FOR_PLAN, STATE_PLAN_APPROVED)
    with pytest.raises(StateError):
        check_transition(STATE_REJECTED, STATE_PLAN_APPROVED)


def test_an_escalated_task_cannot_jump_forward() -> None:
    """The illegal path the plan names by hand."""
    for target in (STATE_PLAN_PROMOTED, STATE_PLAN_APPROVED):
        with pytest.raises(StateError):
            check_transition(STATE_ESCALATED, target)


def test_a_promoted_plan_does_not_reach_the_second_slice() -> None:
    """AC13, second bullet: downstream transitions are refused while awaiting approval."""
    for target in ("TESTING", "IMPLEMENTING", "REVIEWING", "DONE"):
        with pytest.raises(StateError):
            check_transition(STATE_PLAN_PROMOTED, target)


def test_terminal_and_backward_transitions_stay_refused() -> None:
    with pytest.raises(StateError):
        check_transition(STATE_ABANDONED, STATE_PLAN_APPROVED)
    with pytest.raises(StateError):
        check_transition(STATE_PLAN_APPROVED, STATE_READY_FOR_PLAN)


def test_an_unknown_state_is_named_in_the_error() -> None:
    with pytest.raises(StateError, match="NOT_A_STATE"):
        check_transition("NOT_A_STATE", STATE_ABANDONED)


def test_append_note_keeps_the_existing_text(database: Database) -> None:
    task = add(database)
    force_state(database, task.id, STATE_PLAN_PROMOTED)
    database.mark_accepted(task.id, plan_accepted=False, notes="first")
    updated = database.append_note(task.id, "schema bypassed")
    assert updated.notes == "first\nschema bypassed"


def test_stats_group_by_profile(database: Database) -> None:
    first = add(database, "PROJ-1", profile="work")
    force_state(database, first.id, STATE_PLAN_PROMOTED)
    database.mark_accepted(first.id, plan_accepted=True)
    second = add(database, "PROJ-2", profile="work")
    force_state(database, second.id, STATE_PLAN_PROMOTED)
    database.mark_accepted(second.id, plan_accepted=False, notes="changed")
    third = add(database, "WEB-1", profile="infra", top=False)
    database.mark_abandoned(third.id)

    grouped = {stats.profile: stats for stats in database.stats()}
    assert set(grouped) == {"work", "infra"}
    assert grouped["work"].accepted_as_is == 1
    assert grouped["work"].accepted_modified == 1
    assert grouped["work"].as_is_rate == pytest.approx(0.5)
    assert grouped["infra"].abandoned == 1
    assert grouped["infra"].top_pick_count == 0

    only_default = database.stats("work")
    assert len(only_default) == 1
    assert only_default[0].prepared == 2


def test_average_minutes_to_accept(database: Database) -> None:
    task = add(database)
    force_state(database, task.id, STATE_PLAN_PROMOTED)
    accepted = database.mark_accepted(task.id, plan_accepted=True)

    started = datetime.fromisoformat(accepted.prepared_at) - timedelta(minutes=30)
    database.conn.execute(
        "UPDATE tasks SET prepared_at = ? WHERE id = ?",
        (started.isoformat(timespec="seconds"), task.id),
    )
    database.conn.commit()

    stats = database.stats("work")[0]
    assert stats.average_minutes_to_accept == pytest.approx(30.0, abs=0.5)


def test_timestamps_are_utc(database: Database) -> None:
    task = add(database)
    parsed = datetime.fromisoformat(task.prepared_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == UTC.utcoffset(None)


# --------------------------------------------------------------------------- #
# LF-5: the columns this slice adds
# --------------------------------------------------------------------------- #

NEW_TASK_COLUMNS = (
    "base_commit",
    "attempt",
    "ticket_attempts",
    "escalation_reason",
    "approved_via",
)


def test_a_new_database_has_the_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "lazyfish.db"
    Database(path).initialise()
    assert set(NEW_TASK_COLUMNS) <= columns_of(path)


def test_a_new_database_has_an_artifacts_table(tmp_path: Path) -> None:
    """D8: lineage and metadata live in the database, content lives in the store."""
    path = tmp_path / "lazyfish.db"
    Database(path).initialise()
    assert "artifacts" in table_names(path)
    assert {
        "id",
        "task_id",
        "type",
        "produced_by",
        "base_commit",
        "parents",
        "promoted_at",
        "attempt",
    } <= columns_of(path, "artifacts")


def test_a_prepared_task_stores_its_baseline(database: Database) -> None:
    task = add(database, base_commit="a" * 40)
    assert task.base_commit == "a" * 40
    assert database.get(task.id).base_commit == "a" * 40


def test_a_task_starts_with_no_attempts_and_no_escalation(database: Database) -> None:
    task = add(database)
    assert task.attempt == 0
    assert task.ticket_attempts == 0
    assert task.escalation_reason is None
    assert task.approved_via is None


def test_a_baseline_may_be_absent(database: Database) -> None:
    """The rows written before this slice have none, and they must still load (AC11)."""
    task = add(database, base_commit=None)
    assert task.base_commit is None


# --------------------------------------------------------------------------- #
# AC7: migrating a database that already has data in it
# --------------------------------------------------------------------------- #


def test_a_database_missing_the_columns_gains_them(tmp_path: Path) -> None:
    """AC7, third shape: the one on the user's disk."""
    path = write_legacy_database(tmp_path / "lazyfish.db")
    assert not set(NEW_TASK_COLUMNS) <= columns_of(path)

    Database(path).initialise()
    assert set(NEW_TASK_COLUMNS) <= columns_of(path)
    assert "artifacts" in table_names(path)


def test_migrating_keeps_every_existing_row_untouched(tmp_path: Path) -> None:
    """R2: five rows of real data, explicitly kept rather than rebuilt."""
    path = write_legacy_database(tmp_path / "lazyfish.db")
    before = legacy_rows_of(path)
    assert len(before) == len(LEGACY_ROWS)

    Database(path).initialise()
    assert legacy_rows_of(path) == before


def test_migrating_twice_changes_nothing(tmp_path: Path) -> None:
    """AC7, second shape: a database that already has the columns."""
    path = write_legacy_database(tmp_path / "lazyfish.db")
    Database(path).initialise()
    after_first = legacy_rows_of(path)
    columns = columns_of(path)

    for _ in range(2):
        instance = Database(path)
        instance.initialise()
        instance.close()

    assert legacy_rows_of(path) == after_first
    assert columns_of(path) == columns


def test_migrated_rows_read_back_as_tasks(tmp_path: Path) -> None:
    """The added columns have to have a usable value on old rows, not crash on read."""
    path = write_legacy_database(tmp_path / "lazyfish.db")
    database = Database(path)
    database.initialise()
    try:
        tasks = database.list_tasks()
        assert [task.ticket_key for task in tasks] == [row[0] for row in LEGACY_ROWS]
        for task in tasks:
            assert task.base_commit is None
            assert task.attempt == 0
            assert task.ticket_attempts == 0
            assert task.escalation_reason is None
            assert task.approved_via is None
    finally:
        database.close()


def test_the_in_flight_index_survives_the_migration(tmp_path: Path) -> None:
    """Trap 2: the guarantee is structural, and must stay structural.

    One live task per ticket, not one per profile (LF-6 D8/D9). The legacy data
    has one READY_FOR_PLAN row, so after migration another ticket in that profile
    is accepted, and a second live copy of that same ticket is refused by the
    database itself.

    The refusal is provoked with raw SQL, which is the whole point: going through
    insert_task would stop at its own pre-check and never reach the index, and
    "the code remembered to look" is a weaker claim than "the database said no".
    Distinct from the same rule on a fresh database (AC12) in that it is the row
    the migration carried over that is being protected.
    """
    path = write_legacy_database(tmp_path / "lazyfish.db")
    database = Database(path)
    database.initialise()
    try:
        assert add(database, "CS-999", profile="spendwatt").ticket_key == "CS-999"
        with pytest.raises(sqlite3.IntegrityError):
            database.conn.execute(
                """INSERT INTO tasks (ticket_key, ticket_title, profile, state, branch,
                   worktree_path, artifacts_path, was_top_pick, prepared_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "CS-370",
                    "a second live copy",
                    "spendwatt",
                    STATE_PLAN_PROMOTED,
                    "lazyfish/CS-370-again",
                    "/tmp/again",
                    "/tmp/again/artifacts",
                    1,
                    "2026-08-02T09:00:00+00:00",
                ),
            )
    finally:
        database.close()


def test_the_migrated_in_flight_row_can_still_be_worked_on(tmp_path: Path) -> None:
    """AC11's row: the one ticket actually in flight has to keep moving."""
    path = write_legacy_database(tmp_path / "lazyfish.db")
    database = Database(path)
    database.initialise()
    try:
        task = database.get_in_flight("spendwatt")
        assert task is not None
        assert task.ticket_key == "CS-370"
        force_state(database, task.id, STATE_PLAN_PROMOTED)
        approved = database.mark_accepted(task.id, plan_accepted=True)
        assert approved.state == STATE_PLAN_APPROVED
    finally:
        database.close()
