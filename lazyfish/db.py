"""SQLite persistence. The only module that writes task state.

Design notes:

* One file, standard library only. A user who has run `pipx install lazyfish`
  must not also have to run a database server (A-1).
* WIP=1 per profile is enforced by a partial unique index, not only by a
  check in the CLI. The database, not the caller, is the authority.
* Illegal state transitions raise instead of silently updating rows: an
  ABANDONED task must never become PLAN_APPROVED.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .artifacts import TYPE_REJECTION, Artifact
from .errors import StateError
from .paths import db_path

STATE_READY_FOR_PLAN = "READY_FOR_PLAN"
STATE_PLAN_PROMOTED = "PLAN_PROMOTED"
STATE_PLAN_APPROVED = "PLAN_APPROVED"
STATE_REJECTED = "REJECTED"
STATE_ESCALATED = "ESCALATED"
STATE_ABANDONED = "ABANDONED"
"""The state machine's vocabulary.

`PLAN_APPROVED` keeps the name and the meaning LF-1 gave it -- the state after a
person approved -- and `PLAN_PROMOTED` is inserted in front of it rather than
renaming anything (Q2, trap 2). The two are different transitions, not aliases:

    promote   the Orchestrator judges the artifact against its contract.
              Deterministic, and therefore automatable.
    approve   a person judges whether the plan is right. Never automatic (D10).

Renaming `PLAN_APPROVED` would have moved the WIP partial unique index, which is
built on `state = 'READY_FOR_PLAN'` and is a structural guarantee rather than an
application-level check. Inserting a state leaves the index untouched.
"""

ACTIVE_STATES = (STATE_READY_FOR_PLAN, STATE_PLAN_APPROVED)

APPROVAL_INTERACTIVE = "interactive"
APPROVAL_NON_INTERACTIVE = "non-interactive"
"""How an approval happened (AC14).

Approval is the one transition that is never automated (D10), so the core records
which way it was reached: a person answering the question at a prompt, or a flag
on the command line. `accept --as-is` is a legitimate thing for a person to type,
but it is also the shape a script would use, and recording the difference keeps
that visible in the data instead of indistinguishable from a considered answer.
The same technique as the `--force` bypass note: make the degenerate path
legible rather than forbidding it.
"""

# ABANDONED is a legal target from every other state (D7), and R4's cleanup path
# depends on it: a command whose job is to tidy up must not itself get stuck.
# Listed in each row rather than special-cased in check_transition, so the table
# stays the single description of the machine.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    # The edge LF-1 had here, READY_FOR_PLAN -> PLAN_APPROVED, is deliberately
    # gone (D7): a plan cannot be approved before it has been promoted, and the
    # order is enforced by the table rather than by whoever remembers it (AC13).
    STATE_READY_FOR_PLAN: frozenset(
        {STATE_PLAN_PROMOTED, STATE_REJECTED, STATE_ESCALATED, STATE_ABANDONED}
    ),
    # A rejection is the retry loop, not the end of a ticket: the next attempt
    # promotes from here. READY_FOR_PLAN is reachable again so that routing can
    # send the ticket back for a fresh attempt.
    STATE_REJECTED: frozenset(
        {STATE_PLAN_PROMOTED, STATE_READY_FOR_PLAN, STATE_ESCALATED, STATE_ABANDONED}
    ),
    # Awaiting approval. PLAN_APPROVED is the person saying yes; READY_FOR_PLAN
    # is the person saying no, which is AC15's third exit. Nothing downstream is
    # reachable from here, which is what makes the approval gate a real gate.
    STATE_PLAN_PROMOTED: frozenset({STATE_PLAN_APPROVED, STATE_READY_FOR_PLAN, STATE_ABANDONED}),
    STATE_PLAN_APPROVED: frozenset({STATE_ABANDONED}),
    # Escalation is a stop, not a step: a person decides what happens next, and
    # the only move the tool itself offers is to abandon the ticket.
    STATE_ESCALATED: frozenset({STATE_ABANDONED}),
    STATE_ABANDONED: frozenset(),
}

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key     TEXT    NOT NULL,
    ticket_title   TEXT    NOT NULL,
    profile   TEXT    NOT NULL,
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
    abandoned_at   TEXT,
    -- The commit a workspace is materialised from. Read from here rather than
    -- from git, because HEAD and refs are writable by the party being checked
    -- (D4). NULL on rows written before this slice; see AC11 for what promote
    -- does with that.
    base_commit    TEXT,
    -- Agent runs, at the two levels D6 specifies. `attempt` is this stage's
    -- count and resets when the stage does; `ticket_attempts` is the total and
    -- never resets, which is the ceiling that stops one pathological ticket
    -- from eating a week of quota.
    attempt          INTEGER NOT NULL DEFAULT 0,
    ticket_attempts  INTEGER NOT NULL DEFAULT 0,
    -- Why the ticket escalated. Distinct from "waiting for approval", which is
    -- a state and not a failure.
    escalation_reason TEXT,
    -- How the approval was reached (AC14).
    approved_via      TEXT
);

-- At most one ticket per profile may be awaiting a plan (R4). Only
-- READY_FOR_PLAN counts: once a plan is recorded lazyfish's part is over, and
-- holding the profile hostage while the human implements would leave no way to
-- start the next ticket short of abandoning a task that was never abandoned.
--
-- LF-5 inserts states rather than renaming any, precisely so that this index
-- does not move: it is a structural guarantee, and rewriting it would demote
-- the WIP limit to an application-level check (trap 2).
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_in_flight_profile
    ON tasks (profile) WHERE state = 'READY_FOR_PLAN';

CREATE INDEX IF NOT EXISTS ix_tasks_ticket ON tasks (ticket_key, profile);

-- Artifact metadata and lineage. The content itself lives in the artifact store
-- under the data directory, addressed by (task_id, id) -- never in the target
-- repository, which is the agent's workspace and not the tool's storage (D8).
--
-- Keyed on both columns rather than on the id alone: the id is a digest of the
-- content, so two tasks whose plans happen to be identical would collide on a
-- single-column key, and the store's layout is per task in any case.
CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT    NOT NULL,
    task_id     INTEGER NOT NULL REFERENCES tasks (id),
    type        TEXT    NOT NULL,
    produced_by TEXT    NOT NULL,
    base_commit TEXT,
    -- The artifact ids this one was made from, as a JSON array. Lineage is the
    -- only answer to "which version of the input was this made against", which
    -- a retry makes a real question.
    parents     TEXT    NOT NULL DEFAULT '[]',
    -- NULL until the Orchestrator promotes it. An artifact exists as soon as it
    -- is extracted; promotion is a separate fact about it.
    promoted_at TEXT,
    promoted_by TEXT,
    attempt     INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT    NOT NULL,
    PRIMARY KEY (task_id, id)
);

CREATE INDEX IF NOT EXISTS ix_artifacts_task ON artifacts (task_id, type);
"""

TASK_COLUMNS_ADDED_BY_LF5: tuple[tuple[str, str], ...] = (
    ("base_commit", "base_commit TEXT"),
    ("attempt", "attempt INTEGER NOT NULL DEFAULT 0"),
    ("ticket_attempts", "ticket_attempts INTEGER NOT NULL DEFAULT 0"),
    ("escalation_reason", "escalation_reason TEXT"),
    ("approved_via", "approved_via TEXT"),
)
"""Columns to add to a `tasks` table that predates LF-5.

Name paired with its DDL fragment so the same list drives both the check and the
statement. Every entry is either nullable or has a default, which is not a style
choice: SQLite cannot add a NOT NULL column without a default to a table that
already has rows. That is also why a migrated row's `base_commit` is NULL rather
than something invented, and why AC11 exists to say what promote does about it.
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
    profile: str
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
    base_commit: str | None = None
    attempt: int = 0
    ticket_attempts: int = 0
    escalation_reason: str | None = None
    approved_via: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        return cls(
            id=row["id"],
            ticket_key=row["ticket_key"],
            ticket_title=row["ticket_title"],
            profile=row["profile"],
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
            base_commit=row["base_commit"],
            attempt=row["attempt"],
            ticket_attempts=row["ticket_attempts"],
            escalation_reason=row["escalation_reason"],
            approved_via=row["approved_via"],
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
    """Aggregates for one profile, or for all profiles combined."""

    profile: str
    prepared: int
    accepted_as_is: int
    accepted_modified: int
    abandoned: int
    in_flight: int
    top_pick_count: int
    average_minutes_to_accept: float | None
    # LF-5's counts. `rejected` is rejections, not tickets: one ticket can be
    # turned down several times, and the retry loop is what the number is about.
    # It totals both sources, a failed contract and a person saying no, because
    # both are a plan that did not survive.
    rejected: int = 0
    escalated: int = 0
    attempts: int = 0

    @property
    def accepted(self) -> int:
        return self.accepted_as_is + self.accepted_modified

    @property
    def as_is_rate(self) -> float | None:
        """Share of accepted plans taken without modification."""
        if self.accepted == 0:
            return None
        return self.accepted_as_is / self.accepted


def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    """Rebuild an Artifact from its metadata row.

    The dataclass lives in `artifacts.py` rather than being duplicated here: an
    artifact read out of the database and one just extracted from a workspace are
    the same kind of thing, and two representations would drift.
    """
    return Artifact(
        id=row["id"],
        type=row["type"],
        produced_by=row["produced_by"],
        task_id=row["task_id"],
        base_commit=row["base_commit"],
        parents=tuple(json.loads(row["parents"] or "[]")),
        promoted_at=row["promoted_at"],
        attempt=row["attempt"],
        promoted_by=row["promoted_by"],
    )


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
        """Create tables and indexes, and bring an older file up to date.

        Safe to run repeatedly, and safe to run on a database that has data in
        it. Two halves, in this order:

        1. `executescript(SCHEMA_SQL)` -- every statement is `IF NOT EXISTS`, so
           this creates what is missing and leaves what exists alone.
        2. `_add_missing_task_columns()` -- because step 1 does *not* reach a
           table that exists but lacks columns. `CREATE TABLE IF NOT EXISTS`
           says nothing about the shape of the table it found, so without this
           an older file would open cleanly and then fail on the first query
           naming a new column.
        """
        with self.conn:
            self.conn.executescript(SCHEMA_SQL)
            self._add_missing_task_columns()
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _add_missing_task_columns(self) -> None:
        """Add any LF-5 column the `tasks` table does not have yet.

        This is the project's first migration, so the shape is worth stating
        plainly -- the next one should look like this:

        - **Ask, do not assume.** `PRAGMA table_info` reports the columns that
          are actually there. Nothing is derived from a version number; the
          stored `user_version` has never been read (a debt LF-4 picks up), and
          a migration that trusts it would be wrong on any file written by a
          build that crashed halfway.
        - **Idempotent by construction.** Adding only what is absent means the
          second run finds nothing to do. There is no "have I run this yet"
          bookkeeping to get out of step with reality.
        - **Additive only.** No column is dropped, renamed or rewritten, and no
          row is touched, so existing data cannot be lost by this path. Any
          future migration that genuinely needs to rewrite rows should copy into
          a new table inside one transaction rather than editing in place.
        """
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        for name, definition in TASK_COLUMNS_ADDED_BY_LF5:
            if name not in existing:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {definition}")

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
        profile: str,
        branch: str,
        worktree_path: str,
        artifacts_path: str,
        was_top_pick: bool,
        base_commit: str | None = None,
    ) -> Task:
        active = self.get_in_flight(profile)
        if active is not None:
            raise StateError(
                f"Profile '{profile}' is already waiting for a plan for "
                f"{active.ticket_key}. Record it with 'lazyfish accept' or drop it "
                f"with 'lazyfish abandon' before preparing another ticket."
            )
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO tasks (
                    ticket_key, ticket_title, profile, state, branch,
                    worktree_path, artifacts_path, was_top_pick, prepared_at,
                    base_commit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_key,
                    ticket_title,
                    profile,
                    STATE_READY_FOR_PLAN,
                    branch,
                    worktree_path,
                    artifacts_path,
                    int(was_top_pick),
                    utc_now(),
                    base_commit,
                ),
            )
        task = self.get(int(cursor.lastrowid))
        assert task is not None  # just inserted
        return task

    def mark_accepted(
        self,
        task_id: int,
        *,
        plan_accepted: bool,
        notes: str | None = None,
        approved_via: str | None = None,
    ) -> Task:
        """Record a person's approval of the plan.

        Only reachable from `PLAN_PROMOTED`: the transition table no longer has
        an edge from `READY_FOR_PLAN`, so a plan that has not passed its contract
        cannot be approved (AC13). Nobody should be asked to read a plan that
        does not even parse.

        `approved_via` records how the decision was made, not what it was.
        """
        task = self._require(task_id)
        check_transition(task.state, STATE_PLAN_APPROVED)
        with self.conn:
            self.conn.execute(
                """
                UPDATE tasks
                   SET state = ?, plan_accepted = ?, notes = ?, accepted_at = ?,
                       approved_via = ?
                 WHERE id = ?
                """,
                (
                    STATE_PLAN_APPROVED,
                    int(plan_accepted),
                    notes,
                    utc_now(),
                    approved_via,
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

    def count_attempt(self, task_id: int) -> Task:
        """Charge one agent run against both budgets (D6).

        Called once per promotion attempt, whatever the outcome. Both counters
        move together here; they differ in that a future stage change resets
        `attempt` and never resets `ticket_attempts`.
        """
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET attempt = attempt + 1, ticket_attempts = ticket_attempts + 1 "
                "WHERE id = ?",
                (task_id,),
            )
        return self._require(task_id)

    def set_state(
        self,
        task_id: int,
        target: str,
        *,
        escalation_reason: str | None = None,
        notes: str | None = None,
    ) -> Task:
        """Move a task to `target`, refusing anything the table does not allow.

        The Orchestrator decides what should happen; this refuses what must not.
        Keeping the check here rather than at the call site means no caller can
        reach an illegal state by forgetting to ask -- the same reason the WIP
        limit is an index instead of an `if`.
        """
        task = self._require(task_id)
        # A move to the state a task is already in is not a transition, so the
        # table is not asked about it. A second failed attempt leaves a ticket
        # rejected, which is correct and not an edge -- recording REJECTED ->
        # REJECTED in the table would describe a loop the machine does not have.
        if target != task.state:
            check_transition(task.state, target)
        merged = f"{task.notes}\n{notes}" if task.notes and notes else (notes or task.notes)
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET state = ?, escalation_reason = ?, notes = ? WHERE id = ?",
                (target, escalation_reason, merged, task_id),
            )
        return self._require(task_id)

    def record_artifact(self, artifact: Artifact) -> Artifact:
        """Store an artifact's metadata. Idempotent for the same (task, id).

        Recorded whether or not it goes on to be promoted: a rejection names the
        artifact it judged, so the judged version has to exist as a row even when
        it failed. That is what lets a second attempt's rejection point at a
        different plan than the first one's.
        """
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO artifacts (
                    id, task_id, type, produced_by, base_commit, parents,
                    promoted_at, promoted_by, attempt, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (task_id, id) DO NOTHING
                """,
                (
                    artifact.id,
                    artifact.task_id,
                    artifact.type,
                    artifact.produced_by,
                    artifact.base_commit,
                    json.dumps(list(artifact.parents)),
                    artifact.promoted_at,
                    artifact.promoted_by,
                    artifact.attempt,
                    utc_now(),
                ),
            )
        return self._require_artifact(artifact.task_id, artifact.id)

    def promote_artifact(self, task_id: int, artifact_id: str, *, promoted_by: str) -> Artifact:
        """Mark an artifact as promoted, and by whom.

        `promoted_by` is not decoration. Promotion is the Orchestrator's
        authority (D2); a person overriding the contract with `--force` is
        recorded as the promoter instead, so the override reads as what it is
        rather than as a clean pass.
        """
        with self.conn:
            self.conn.execute(
                "UPDATE artifacts SET promoted_at = ?, promoted_by = ? "
                "WHERE task_id = ? AND id = ?",
                (utc_now(), promoted_by, task_id, artifact_id),
            )
        return self._require_artifact(task_id, artifact_id)

    def _require_artifact(self, task_id: int, artifact_id: str) -> Artifact:
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? AND id = ?", (task_id, artifact_id)
        ).fetchone()
        if row is None:
            raise StateError(f"No artifact {artifact_id} for task {task_id}.")
        return _artifact_from_row(row)

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

    def list_artifacts(self, task_id: int) -> list[Artifact]:
        """Artifact metadata for one task, oldest first.

        Ordered by rowid rather than by `recorded_at`: timestamps are recorded to
        the second, so two artifacts written in the same second would tie, and
        the order of attempts is exactly what lineage questions depend on.
        """
        rows = self.conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY rowid", (task_id,)
        )
        return [_artifact_from_row(row) for row in rows]

    def _require(self, task_id: int) -> Task:
        task = self.get(task_id)
        if task is None:
            raise StateError(f"No task with id {task_id}.")
        return task

    def get_in_flight(self, profile: str) -> Task | None:
        """The task awaiting a plan for a profile. At most one by construction."""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE profile = ? AND state = ? ORDER BY id DESC LIMIT 1",
            (profile, STATE_READY_FOR_PLAN),
        ).fetchone()
        return Task.from_row(row) if row else None

    def get_open(self, profile: str) -> Task | None:
        """The most recent task that has not been abandoned.

        Used by `abandon`, which must also be able to clean up the worktree of a
        task whose plan was already recorded.
        """
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE profile = ? AND state <> ? ORDER BY id DESC LIMIT 1",
            (profile, STATE_ABANDONED),
        ).fetchone()
        return Task.from_row(row) if row else None

    def get_by_state(self, state: str, profile: str | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks WHERE state = ?"
        params: list[object] = [state]
        if profile:
            sql += " AND profile = ?"
            params.append(profile)
        sql += " ORDER BY id"
        return [Task.from_row(row) for row in self.conn.execute(sql, params)]

    def get_by_ticket(self, ticket_key: str, profile: str) -> Task | None:
        """Most recent row for a ticket in a profile, whatever its state."""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE ticket_key = ? AND profile = ? ORDER BY id DESC LIMIT 1",
            (ticket_key, profile),
        ).fetchone()
        return Task.from_row(row) if row else None

    def get_states_for(self, ticket_keys: list[str], profile: str) -> dict[str, str]:
        """Local state of each of `ticket_keys`, for the ones lazyfish knows.

        One query rather than one per ticket: `list` annotates a whole page of
        candidates, and a round trip each would be a needless multiple of the
        work. Keys with no row are simply absent from the result.
        """
        if not ticket_keys:
            return {}
        placeholders = ", ".join("?" for _ in ticket_keys)
        rows = self.conn.execute(
            f"SELECT ticket_key, state FROM tasks "
            f"WHERE profile = ? AND ticket_key IN ({placeholders}) "
            f"ORDER BY id",
            (profile, *ticket_keys),
        )
        # Ordered by id, so a later row for the same ticket wins: the current
        # state, not the first one it ever had.
        return {row["ticket_key"]: row["state"] for row in rows}

    def list_tasks(self, profile: str | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks"
        params: list[object] = []
        if profile:
            sql += " WHERE profile = ?"
            params.append(profile)
        sql += " ORDER BY id"
        return [Task.from_row(row) for row in self.conn.execute(sql, params)]

    def stats(self, profile: str | None = None) -> list[ProfileStats]:
        """Per-profile aggregates, ordered by profile name.

        The averages are computed in Python rather than SQL because the
        timestamps are ISO strings and a wrong-but-plausible SQL date expression
        would corrupt the one number this slice exists to produce.
        """
        by_profile: dict[str, list[Task]] = {}
        for task in self.list_tasks(profile):
            by_profile.setdefault(task.profile, []).append(task)

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
                    profile=profile,
                    prepared=len(tasks),
                    accepted_as_is=sum(1 for t in tasks if t.plan_accepted is True),
                    accepted_modified=sum(1 for t in tasks if t.plan_accepted is False),
                    abandoned=sum(1 for t in tasks if t.state == STATE_ABANDONED),
                    in_flight=sum(1 for t in tasks if t.state == STATE_READY_FOR_PLAN),
                    top_pick_count=sum(1 for t in tasks if t.was_top_pick),
                    average_minutes_to_accept=(
                        sum(durations) / len(durations) if durations else None
                    ),
                    rejected=sum(self._count_rejections(task.id) for task in tasks),
                    escalated=sum(1 for t in tasks if t.state == STATE_ESCALATED),
                    attempts=sum(t.attempt for t in tasks),
                )
            )
        return result

    def _count_rejections(self, task_id: int) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM artifacts WHERE task_id = ? AND type = ?",
            (task_id, TYPE_REJECTION),
        ).fetchone()
        return int(row[0])


@contextmanager
def open_db(path: Path | None = None) -> Iterator[Database]:
    """Open (and create if needed) the task database."""
    database = Database(path or db_path())
    database.initialise()
    try:
        yield database
    finally:
        database.close()
