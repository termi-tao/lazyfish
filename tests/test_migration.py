"""What happens to a database written by an earlier build.

Written before the implementation, and it has to be: these tests assert on the
handling of data that already exists, and running the migration once destroys
the evidence. The old schema is inlined below rather than reconstructed from
`db.SCHEMA_SQL` -- history does not change, so a copy of it is more honest than
a parameterised current schema pretending to be its own ancestor.

The user has real work on another machine: a batch of finished tickets and
possibly some in flight. AC3 is the one that protects it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lazyfish.db import (
    STATE_ABANDONED,
    STATE_APPROVED,
    STATE_AWAITING_ARTIFACT,
    STATE_COMPLETED,
    STATE_PROMOTED,
    STATE_REJECTED,
    Database,
)

# --------------------------------------------------------------------------- #
# The schema as LF-4 left it
# --------------------------------------------------------------------------- #

OLD_SCHEMA_VERSION = 1

OLD_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key       TEXT    NOT NULL,
    ticket_title     TEXT    NOT NULL,
    profile          TEXT    NOT NULL,
    state            TEXT    NOT NULL,
    branch           TEXT    NOT NULL,
    worktree_path    TEXT    NOT NULL,
    artifacts_path   TEXT    NOT NULL,
    base_commit      TEXT,
    was_top_pick     INTEGER NOT NULL DEFAULT 0,
    plan_accepted    INTEGER,
    approved_via     TEXT,
    notes            TEXT,
    drift_files      INTEGER,
    drift_lines      INTEGER,
    escalation_reason TEXT,
    attempt          INTEGER NOT NULL DEFAULT 0,
    ticket_attempts  INTEGER NOT NULL DEFAULT 0,
    prepared_at      TEXT    NOT NULL,
    accepted_at      TEXT,
    abandoned_at     TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id           TEXT    NOT NULL,
    type         TEXT    NOT NULL,
    produced_by  TEXT    NOT NULL,
    call_site    TEXT,
    task_id      INTEGER NOT NULL REFERENCES tasks(id),
    base_commit  TEXT,
    attempt      INTEGER NOT NULL DEFAULT 0,
    promoted_by  TEXT,
    promoted_at  TEXT,
    created_at   TEXT    NOT NULL,
    PRIMARY KEY (task_id, id)
);
"""

OLD_LIVE_STATES = ("READY_FOR_PLAN", "PLAN_PROMOTED", "REJECTED", "ESCALATED")

OLD_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_live_ticket "
    "ON tasks (profile, ticket_key) WHERE state IN ("
    + ", ".join(f"'{state}'" for state in OLD_LIVE_STATES)
    + ")"
)


def old_database(path: Path, rows: list[dict[str, object]]) -> None:
    """Build a database exactly as the previous build would have left it."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(OLD_SCHEMA_SQL)
        conn.execute(OLD_INDEX_SQL)
        conn.execute(f"PRAGMA user_version = {OLD_SCHEMA_VERSION}")
        for row in rows:
            columns = ", ".join(row)
            placeholders = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT INTO tasks ({columns}) VALUES ({placeholders})", tuple(row.values())
            )
        conn.commit()
    finally:
        conn.close()


def task_row(key: str, state: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ticket_key": key,
        "ticket_title": f"{key} title",
        "profile": "work",
        "state": state,
        "branch": f"lazyfish/{key}",
        "worktree_path": f"/tmp/wt/work/{key}/architect",
        "artifacts_path": f"/tmp/wt/work/{key}/architect/artifacts/{key}",
        "base_commit": "0" * 40,
        "prepared_at": "2026-08-01T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def states_of(path: Path) -> dict[str, tuple[str, str | None]]:
    """{ticket_key: (state, current_stage)} read with raw sqlite, not the ORM."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            row["ticket_key"]: (row["state"], row["current_stage"])
            for row in conn.execute("SELECT * FROM tasks")
        }
    finally:
        conn.close()


def user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    """One row of every shape that matters, in the old vocabulary."""
    path = tmp_path / "lazyfish.db"
    old_database(
        path,
        [
            task_row("PROJ-1", "READY_FOR_PLAN"),
            task_row("PROJ-2", "PLAN_PROMOTED"),
            task_row("PROJ-3", "REJECTED", attempt=2, ticket_attempts=2),
            task_row(
                "PROJ-4",
                "PLAN_APPROVED",
                plan_accepted=1,
                approved_via="interactive",
                accepted_at="2026-08-02T11:00:00+00:00",
            ),
            task_row("PROJ-5", "ESCALATED", escalation_reason="stage-budget-exhausted"),
            task_row("PROJ-6", "ABANDONED", abandoned_at="2026-08-03T09:00:00+00:00"),
        ],
    )
    return path


def migrate(path: Path) -> None:
    database = Database(path)
    try:
        database.initialise()
    finally:
        database.close()


# --------------------------------------------------------------------------- #
# AC2: the migration does not break work in flight
# --------------------------------------------------------------------------- #


def test_in_flight_rows_keep_flying(legacy_db: Path) -> None:
    migrate(legacy_db)
    states = states_of(legacy_db)

    assert states["PROJ-1"] == (STATE_AWAITING_ARTIFACT, "architect")
    assert states["PROJ-2"] == (STATE_PROMOTED, "architect")
    assert states["PROJ-3"] == (STATE_REJECTED, "architect")


def test_the_migration_touches_nothing_but_the_state(legacy_db: Path) -> None:
    """Paths especially: a moved worktree path is a broken working directory."""
    before = sqlite3.connect(legacy_db)
    before.row_factory = sqlite3.Row
    original = {row["ticket_key"]: dict(row) for row in before.execute("SELECT * FROM tasks")}
    before.close()

    migrate(legacy_db)

    after = sqlite3.connect(legacy_db)
    after.row_factory = sqlite3.Row
    for row in after.execute("SELECT * FROM tasks"):
        was = original[row["ticket_key"]]
        for column in was:
            if column == "state":
                continue
            assert row[column] == was[column], f"{row['ticket_key']}.{column} changed"
    after.close()


def test_live_rows_are_still_live(legacy_db: Path) -> None:
    migrate(legacy_db)
    database = Database(legacy_db)
    try:
        live = {task.ticket_key for task in database.get_live("work")}
    finally:
        database.close()

    assert live == {"PROJ-1", "PROJ-2", "PROJ-3", "PROJ-5"}


# --------------------------------------------------------------------------- #
# AC3: finished tickets must not come back to life
# --------------------------------------------------------------------------- #


def test_plan_approved_becomes_completed_not_approved(legacy_db: Path) -> None:
    """The user has a batch of these on another machine (D3).

    Translating PLAN_APPROVED to APPROVED would mean every finished ticket is
    suddenly waiting for the next stage, with no rule broken anywhere.
    """
    migrate(legacy_db)
    state, stage = states_of(legacy_db)["PROJ-4"]

    assert state == STATE_COMPLETED
    assert state != STATE_APPROVED
    assert stage == "architect"


def test_a_completed_ticket_is_not_live_and_has_no_next_step(legacy_db: Path) -> None:
    from lazyfish.orchestrator import next_step

    migrate(legacy_db)
    database = Database(legacy_db)
    try:
        assert all(task.ticket_key != "PROJ-4" for task in database.get_live("work"))
        completed = database.get_by_ticket("PROJ-4", "work")
    finally:
        database.close()

    assert completed is not None
    step = next_step(completed)
    assert step.stage is None


def test_terminal_states_are_left_alone(legacy_db: Path) -> None:
    migrate(legacy_db)
    states = states_of(legacy_db)

    assert states["PROJ-5"][0] == "ESCALATED"
    assert states["PROJ-6"][0] == STATE_ABANDONED


# --------------------------------------------------------------------------- #
# AC7: the structural guarantee survives the rename
# --------------------------------------------------------------------------- #


def test_the_live_ticket_index_still_refuses_a_duplicate(legacy_db: Path) -> None:
    """R2: an index whose WHERE clause names the old states matches nothing.

    Nothing else would notice -- every other test would stay green, because no
    other test inserts a duplicate.
    """
    migrate(legacy_db)
    conn = sqlite3.connect(legacy_db)
    try:
        duplicate = task_row("PROJ-1", STATE_AWAITING_ARTIFACT)
        duplicate["current_stage"] = "architect"
        columns = ", ".join(duplicate)
        placeholders = ", ".join("?" for _ in duplicate)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO tasks ({columns}) VALUES ({placeholders})",
                tuple(duplicate.values()),
            )
            conn.commit()
    finally:
        conn.close()


def test_the_index_predicate_names_the_new_states(legacy_db: Path) -> None:
    """Read the stored index definition, not our idea of it."""
    migrate(legacy_db)
    conn = sqlite3.connect(legacy_db)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'ux_tasks_live_ticket'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert STATE_AWAITING_ARTIFACT in sql
    assert "READY_FOR_PLAN" not in sql
    assert STATE_COMPLETED not in sql


# --------------------------------------------------------------------------- #
# AC8 / AC9: idempotence and the version stamp
# --------------------------------------------------------------------------- #


def test_migrating_three_times_is_the_same_as_once(legacy_db: Path) -> None:
    """`initialise()` runs on every command, so this is the normal case."""
    migrate(legacy_db)
    once = states_of(legacy_db)
    migrate(legacy_db)
    migrate(legacy_db)

    assert states_of(legacy_db) == once


def test_the_version_is_stamped(legacy_db: Path) -> None:
    from lazyfish.db import SCHEMA_VERSION

    assert user_version(legacy_db) == OLD_SCHEMA_VERSION
    migrate(legacy_db)
    assert user_version(legacy_db) == SCHEMA_VERSION
    assert SCHEMA_VERSION > OLD_SCHEMA_VERSION


def test_a_build_that_predates_the_migration_refuses_the_file(legacy_db: Path) -> None:
    """LF-4's guard, now reachable for real: two machines, one data directory."""
    import lazyfish.db as db_module
    from lazyfish.errors import DatabaseError

    migrate(legacy_db)

    current = db_module.SCHEMA_VERSION
    db_module.SCHEMA_VERSION = OLD_SCHEMA_VERSION
    try:
        database = Database(legacy_db)
        with pytest.raises(DatabaseError) as excinfo:
            database.initialise()
        database.close()
    finally:
        db_module.SCHEMA_VERSION = current

    assert "newer lazyfish" in str(excinfo.value)


def test_a_fresh_database_needs_no_migration(tmp_path: Path) -> None:
    """The path most runs take: nothing to translate, still stamped."""
    from lazyfish.db import SCHEMA_VERSION

    path = tmp_path / "fresh.db"
    migrate(path)

    assert user_version(path) == SCHEMA_VERSION
    assert states_of(path) == {}
