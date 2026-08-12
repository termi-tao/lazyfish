"""SQLite persistence. The only module that writes task state.

Design notes:

* One file, standard library only. A user who has run `pipx install lazyfish`
  must not also have to run a database server (A-1).
* WIP=1 per repo profile is enforced by a partial unique index, not only by a
  check in the CLI. The database, not the caller, is the authority.
* Illegal state transitions raise instead of silently updating rows: an
  ABANDONED task must never become PLAN_APPROVED.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .errors import StateError
from .paths import db_path

STATE_READY_FOR_PLAN = "READY_FOR_PLAN"
STATE_PLAN_APPROVED = "PLAN_APPROVED"
STATE_ABANDONED = "ABANDONED"

ACTIVE_STATES = (STATE_READY_FOR_PLAN, STATE_PLAN_APPROVED)

LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_READY_FOR_PLAN: frozenset({STATE_PLAN_APPROVED, STATE_ABANDONED}),
    STATE_PLAN_APPROVED: frozenset({STATE_ABANDONED}),
    STATE_ABANDONED: frozenset(),
}

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key     TEXT    NOT NULL,
    ticket_title   TEXT    NOT NULL,
    repo_profile   TEXT    NOT NULL,
    state          TEXT    NOT NULL,
    branch         TEXT    NOT NULL,
    worktree_path  TEXT    NOT NULL,
    artifacts_path TEXT    NOT NULL,
    -- true when the ticket was the tracker query's first result, i.e. the
    -- automatic choice. Compared against acceptance later to decide whether the
    -- query needs tuning (Q1).
    was_top_pick   INTEGER NOT NULL,
    -- NULL until accept: true when the plan was taken as-is, false when the
    -- human changed it first.
    plan_accepted  INTEGER,
    notes          TEXT,
    prepared_at    TEXT    NOT NULL,
    accepted_at    TEXT,
    abandoned_at   TEXT
);

-- At most one ticket per repo profile may be awaiting a plan (R4). Only
-- READY_FOR_PLAN counts: once a plan is recorded lazyfish's part is over, and
-- holding the profile hostage while the human implements would leave no way to
-- start the next ticket short of abandoning a task that was never abandoned.
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_in_flight_profile
    ON tasks (repo_profile) WHERE state = 'READY_FOR_PLAN';

CREATE INDEX IF NOT EXISTS ix_tasks_ticket ON tasks (ticket_key, repo_profile);
"""


def utc_now() -> str:
    """Timestamps are ISO-8601 in UTC; local time makes durations unreliable."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Task:
    """One row of the tasks table."""

    id: int
    ticket_key: str
    ticket_title: str
    repo_profile: str
    state: str
    branch: str
    worktree_path: str
    artifacts_path: str
    was_top_pick: bool
    plan_accepted: bool | None
    notes: str | None
    prepared_at: str
    accepted_at: str | None
    abandoned_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        return cls(
            id=row["id"],
            ticket_key=row["ticket_key"],
            ticket_title=row["ticket_title"],
            repo_profile=row["repo_profile"],
            state=row["state"],
            branch=row["branch"],
            worktree_path=row["worktree_path"],
            artifacts_path=row["artifacts_path"],
            was_top_pick=bool(row["was_top_pick"]),
            plan_accepted=(None if row["plan_accepted"] is None else bool(row["plan_accepted"])),
            notes=row["notes"],
            prepared_at=row["prepared_at"],
            accepted_at=row["accepted_at"],
            abandoned_at=row["abandoned_at"],
        )

    def minutes_to_accept(self) -> float | None:
        """Wall-clock minutes from prep to accept, or None if not accepted."""
        started = _parse_ts(self.prepared_at)
        finished = _parse_ts(self.accepted_at)
        if started is None or finished is None:
            return None
        return (finished - started).total_seconds() / 60.0


@dataclass(frozen=True)
class ProfileStats:
    """Aggregates for one repo profile, or for all profiles combined."""

    repo_profile: str
    prepared: int
    accepted_as_is: int
    accepted_modified: int
    abandoned: int
    in_flight: int
    top_pick_count: int
    average_minutes_to_accept: float | None

    @property
    def accepted(self) -> int:
        return self.accepted_as_is + self.accepted_modified

    @property
    def as_is_rate(self) -> float | None:
        """Share of accepted plans taken without modification."""
        if self.accepted == 0:
            return None
        return self.accepted_as_is / self.accepted


def check_transition(current: str, target: str) -> None:
    """Raise StateError unless current -> target is a legal transition."""
    allowed = LEGAL_TRANSITIONS.get(current)
    if allowed is None:
        raise StateError(f"Unknown task state in database: {current!r}")
    if target not in allowed:
        legal = ", ".join(sorted(allowed)) or "(none, this is a terminal state)"
        raise StateError(f"Cannot move a task from {current} to {target}. Legal targets: {legal}")


class Database:
    """Thin wrapper over a sqlite3 connection. Callers use the module helpers."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    # -- lifecycle -------------------------------------------------------- #

    def initialise(self) -> None:
        """Create tables and indexes. Safe to run repeatedly."""
        with self.conn:
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- writes ----------------------------------------------------------- #

    def insert_task(
        self,
        *,
        ticket_key: str,
        ticket_title: str,
        repo_profile: str,
        branch: str,
        worktree_path: str,
        artifacts_path: str,
        was_top_pick: bool,
    ) -> Task:
        active = self.get_in_flight(repo_profile)
        if active is not None:
            raise StateError(
                f"Repo profile '{repo_profile}' is already waiting for a plan for "
                f"{active.ticket_key}. Record it with 'lazyfish accept' or drop it "
                f"with 'lazyfish abandon' before preparing another ticket."
            )
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO tasks (
                    ticket_key, ticket_title, repo_profile, state, branch,
                    worktree_path, artifacts_path, was_top_pick, prepared_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_key,
                    ticket_title,
                    repo_profile,
                    STATE_READY_FOR_PLAN,
                    branch,
                    worktree_path,
                    artifacts_path,
                    int(was_top_pick),
                    utc_now(),
                ),
            )
        task = self.get(int(cursor.lastrowid))
        assert task is not None  # just inserted
        return task

    def mark_accepted(self, task_id: int, *, plan_accepted: bool, notes: str | None = None) -> Task:
        """Record the outcome of the design review and close the task."""
        task = self._require(task_id)
        check_transition(task.state, STATE_PLAN_APPROVED)
        with self.conn:
            self.conn.execute(
                """
                UPDATE tasks
                   SET state = ?, plan_accepted = ?, notes = ?, accepted_at = ?
                 WHERE id = ?
                """,
                (
                    STATE_PLAN_APPROVED,
                    int(plan_accepted),
                    notes,
                    utc_now(),
                    task_id,
                ),
            )
        return self._require(task_id)

    def mark_abandoned(self, task_id: int, *, notes: str | None = None) -> Task:
        task = self._require(task_id)
        check_transition(task.state, STATE_ABANDONED)
        merged = notes if notes else task.notes
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET state = ?, notes = ?, abandoned_at = ? WHERE id = ?",
                (STATE_ABANDONED, merged, utc_now(), task_id),
            )
        return self._require(task_id)

    def append_note(self, task_id: int, note: str) -> Task:
        """Append a line to notes, used for things like a schema bypass (R3)."""
        task = self._require(task_id)
        merged = f"{task.notes}\n{note}" if task.notes else note
        with self.conn:
            self.conn.execute("UPDATE tasks SET notes = ? WHERE id = ?", (merged, task_id))
        return self._require(task_id)

    # -- reads ------------------------------------------------------------ #

    def get(self, task_id: int) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.from_row(row) if row else None

    def _require(self, task_id: int) -> Task:
        task = self.get(task_id)
        if task is None:
            raise StateError(f"No task with id {task_id}.")
        return task

    def get_in_flight(self, repo_profile: str) -> Task | None:
        """The task awaiting a plan for a profile. At most one by construction."""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE repo_profile = ? AND state = ? ORDER BY id DESC LIMIT 1",
            (repo_profile, STATE_READY_FOR_PLAN),
        ).fetchone()
        return Task.from_row(row) if row else None

    def get_open(self, repo_profile: str) -> Task | None:
        """The most recent task that has not been abandoned.

        Used by `abandon`, which must also be able to clean up the worktree of a
        task whose plan was already recorded.
        """
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE repo_profile = ? AND state <> ? ORDER BY id DESC LIMIT 1",
            (repo_profile, STATE_ABANDONED),
        ).fetchone()
        return Task.from_row(row) if row else None

    def get_by_state(self, state: str, repo_profile: str | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks WHERE state = ?"
        params: list[object] = [state]
        if repo_profile:
            sql += " AND repo_profile = ?"
            params.append(repo_profile)
        sql += " ORDER BY id"
        return [Task.from_row(row) for row in self.conn.execute(sql, params)]

    def get_by_ticket(self, ticket_key: str, repo_profile: str) -> Task | None:
        """Most recent row for a ticket in a profile, whatever its state."""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE ticket_key = ? AND repo_profile = ? "
            "ORDER BY id DESC LIMIT 1",
            (ticket_key, repo_profile),
        ).fetchone()
        return Task.from_row(row) if row else None

    def list_tasks(self, repo_profile: str | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks"
        params: list[object] = []
        if repo_profile:
            sql += " WHERE repo_profile = ?"
            params.append(repo_profile)
        sql += " ORDER BY id"
        return [Task.from_row(row) for row in self.conn.execute(sql, params)]

    def stats(self, repo_profile: str | None = None) -> list[ProfileStats]:
        """Per-profile aggregates, ordered by profile name.

        The averages are computed in Python rather than SQL because the
        timestamps are ISO strings and a wrong-but-plausible SQL date expression
        would corrupt the one number this slice exists to produce.
        """
        by_profile: dict[str, list[Task]] = {}
        for task in self.list_tasks(repo_profile):
            by_profile.setdefault(task.repo_profile, []).append(task)

        result: list[ProfileStats] = []
        for profile in sorted(by_profile):
            tasks = by_profile[profile]
            durations = [
                minutes
                for minutes in (task.minutes_to_accept() for task in tasks)
                if minutes is not None
            ]
            result.append(
                ProfileStats(
                    repo_profile=profile,
                    prepared=len(tasks),
                    accepted_as_is=sum(1 for t in tasks if t.plan_accepted is True),
                    accepted_modified=sum(1 for t in tasks if t.plan_accepted is False),
                    abandoned=sum(1 for t in tasks if t.state == STATE_ABANDONED),
                    in_flight=sum(1 for t in tasks if t.state == STATE_READY_FOR_PLAN),
                    top_pick_count=sum(1 for t in tasks if t.was_top_pick),
                    average_minutes_to_accept=(
                        sum(durations) / len(durations) if durations else None
                    ),
                )
            )
        return result


@contextmanager
def open_db(path: Path | None = None) -> Iterator[Database]:
    """Open (and create if needed) the task database."""
    database = Database(path or db_path())
    database.initialise()
    try:
        yield database
    finally:
        database.close()
