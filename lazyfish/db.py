"""SQLite persistence. The only module that writes task state.

Design notes:

* One file, standard library only. A user who has run `pipx install lazyfish`
  must not also have to run a database server (A-1).
* One live task per ticket is enforced by a partial unique index, not only by a
  check in the CLI. The database, not the caller, is the authority.
* Illegal state transitions raise instead of silently updating rows: an
  ABANDONED task must never become PLAN_APPROVED.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .artifacts import TYPE_REJECTION, Artifact, ensure_authorized
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

Renaming `PLAN_APPROVED` would have moved the partial unique index below, which
is a structural guarantee rather than an application-level check. Inserting a
state leaves the index untouched; LF-6 changed its key, and did so as a stated
decision with a migration rather than as a side effect of a rename.
"""

ACTIVE_STATES = (STATE_READY_FOR_PLAN, STATE_PLAN_APPROVED)

LIVE_STATES = (
    STATE_READY_FOR_PLAN,
    STATE_PLAN_PROMOTED,
    STATE_REJECTED,
    STATE_ESCALATED,
)
"""The states in which a ticket is still lazyfish's business (LF-6 D9/D10).

The window from prep to a person's decision. `PLAN_APPROVED` is outside it --
once the plan is recorded lazyfish's part is over and the same ticket may be
prepared again -- and so is `ABANDONED`.

This list is what "in flight" now means, and it is read by three things that
must not disagree: the unique index below, the ambiguity check that decides
whether a command needs `--ticket`, and `get_live`. One definition, because
a second copy of it would drift the moment a state is inserted.
"""

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
    approved_via      TEXT,
    -- How far the workspace moved away from base_commit by the time the plan was
    -- promoted (C1). An observation, not a verdict: promotion ignores workspace
    -- changes, and these columns exist so that "the design stage also wrote the
    -- implementation" is a measurable rate rather than an impression. NULL means
    -- not measured, which includes rows written before C1.
    workspace_delta_files INTEGER,
    workspace_delta_lines INTEGER
);

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
    -- Which point in the pipeline produced it. `produced_by` names the role and
    -- cannot answer this: Tester and Reviewer each run at two call sites, and
    -- the two are authorised to produce different types (LF-6 D3). NULL on rows
    -- written before this slice.
    call_site   TEXT,
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

LEGACY_WIP_INDEX = "ux_tasks_in_flight_profile"
LIVE_TICKET_INDEX = "ux_tasks_live_ticket"

_LIVE_STATE_LITERALS = ", ".join(f"'{state}'" for state in LIVE_STATES)

LIVE_TICKET_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {LIVE_TICKET_INDEX} "
    f"ON tasks (profile, ticket_key) WHERE state IN ({_LIVE_STATE_LITERALS})"
)
"""The structural guarantee, rekeyed (D9).

    was     ON tasks (profile)              WHERE state = 'READY_FOR_PLAN'
            one ticket per profile at a time
    now     ON tasks (profile, ticket_key)  WHERE state IN (the live window)
            one live task per ticket

Nothing was given up. The old invariant was quota discipline wearing the costume
of an engineering constraint, and quota is now managed directly by the retry
budgets; working on several tickets at once is normal, and having one ticket
with two worktrees and two branches of the same name never was.

The new key also covers the whole window rather than one state of it, which is
what the old one had quietly stopped doing when LF-5 inserted states around it.
"""

ADDED_TASK_COLUMNS: tuple[tuple[str, str], ...] = (
    # LF-5
    ("base_commit", "base_commit TEXT"),
    ("attempt", "attempt INTEGER NOT NULL DEFAULT 0"),
    ("ticket_attempts", "ticket_attempts INTEGER NOT NULL DEFAULT 0"),
    ("escalation_reason", "escalation_reason TEXT"),
    ("approved_via", "approved_via TEXT"),
    # C1
    ("workspace_delta_files", "workspace_delta_files INTEGER"),
    ("workspace_delta_lines", "workspace_delta_lines INTEGER"),
)
"""Columns added to `tasks` after its first shape, oldest first.

One list rather than one per ticket. The migration only asks which columns are
missing, so it does not care when any of them arrived, and a single list means
the next ticket appends two lines instead of copying the mechanism.

Name paired with its DDL fragment so the same entry drives both the check and the
statement. Every entry is either nullable or has a default, which is not a style
choice: SQLite cannot add a NOT NULL column without a default to a table that
already has rows. That is also why a migrated row's `base_commit` is NULL rather
than something invented, and why AC11 exists to say what promote does about it.
"""

ADDED_ARTIFACT_COLUMNS: tuple[tuple[str, str], ...] = (
    # LF-6
    ("call_site", "call_site TEXT"),
)
"""The same list for `artifacts`, which reached a second shape in LF-6.

A row migrated from LF-5 has no call site, so authority falls back to the
role-level question for it. That is the weaker check, and it is the strongest
one available about a row that never recorded which call site made it.
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
    workspace_delta_files: int | None = None
    workspace_delta_lines: int | None = None

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
            workspace_delta_files=row["workspace_delta_files"],
            workspace_delta_lines=row["workspace_delta_lines"],
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
    # Mean workspace drift over the tasks that were measured (C1). None when
    # none of them were, which is what a database from before C1 looks like.
    average_delta_files: float | None = None
    average_delta_lines: float | None = None

    @property
    def accepted(self) -> int:
        return self.accepted_as_is + self.accepted_modified

    @property
    def as_is_rate(self) -> float | None:
        """Share of accepted plans taken without modification."""
        if self.accepted == 0:
            return None
        return self.accepted_as_is / self.accepted


def _mean(values: Iterable[int | None]) -> float | None:
    """Average the measured values, ignoring the ones that were never measured.

    None rather than zero when nothing was measured: a database with no
    measurements and one whose measurements are all zero are different facts, and
    the second is the interesting one.
    """
    measured = [value for value in values if value is not None]
    if not measured:
        return None
    return sum(measured) / len(measured)


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
        call_site=row["call_site"],
        task_id=row["task_id"],
        base_commit=row["base_commit"],
        parents=tuple(json.loads(row["parents"] or "[]")),
        promoted_at=row["promoted_at"],
        attempt=row["attempt"],
        promoted_by=row["promoted_by"],
    )


def _merge_notes(existing: str | None, addition: str | None) -> str | None:
    """Notes are appended, never replaced (D7).

    The rule in one place because four writers need it and each one of them
    overwriting instead would lose a different thing. Nothing to add leaves the
    column exactly as it was; nothing there yet means the addition is the whole
    value. A note is a log of what happened to a ticket, and a log that keeps
    only the last line is not one.
    """
    if not addition:
        return existing
    if not existing:
        return addition
    return f"{existing}\n{addition}"


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
        2. `_add_missing_columns()` -- because step 1 does *not* reach a
           table that exists but lacks columns. `CREATE TABLE IF NOT EXISTS`
           says nothing about the shape of the table it found, so without this
           an older file would open cleanly and then fail on the first query
           naming a new column.
        3. `_rekey_live_ticket_index()` -- for the same reason one step up: an
           index that already exists under the old key is not touched by a
           `CREATE ... IF NOT EXISTS` for a different one.
        """
        with self.conn:
            self.conn.executescript(SCHEMA_SQL)
            self._add_missing_columns()
            self._rekey_live_ticket_index()
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _rekey_live_ticket_index(self) -> None:
        """Replace the WIP index with the per-ticket one (D9). Idempotent.

        This is the project's first migration of a structural object, and the
        shape it borrows is the column migration's: state what should be true,
        let the statements be no-ops when it already is. SQLite cannot alter an
        index, so the only route is DROP then CREATE, and both halves are
        conditional -- `initialise()` runs on every open, and a second run that
        threw would turn a migration into a one-shot.

        Correct on all three shapes of file: a new database has neither index and
        gets the new one; a current database has it already and nothing happens;
        an older database has the old index, which is dropped, and gains the new
        one. No row is read or written either way.
        """
        self.conn.execute(f"DROP INDEX IF EXISTS {LEGACY_WIP_INDEX}")
        try:
            self.conn.execute(LIVE_TICKET_INDEX_SQL)
        except sqlite3.IntegrityError as exc:
            # Only reachable from a file that already holds two live rows for one
            # ticket, which no version of the application could produce. Said
            # plainly rather than as a raw sqlite message, because the fix is to
            # abandon one of them and there is no way to guess which.
            raise StateError(
                "This database already contains two live tasks for the same ticket, "
                "which the new per-ticket index forbids. Abandon the one you do not "
                f"want to keep, then run the command again. ({exc})"
            ) from exc

    def _add_missing_columns(self) -> None:
        """Add any column the tables do not have yet.

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
        for table, columns in (
            ("tasks", ADDED_TASK_COLUMNS),
            ("artifacts", ADDED_ARTIFACT_COLUMNS),
        ):
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

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
        """Record a newly prepared ticket.

        A profile may hold as many live tickets as the person wants (D8); what it
        may not hold is two live copies of one ticket, because that means two
        worktrees and two branches of the same name racing each other. The index
        is the authority here and this check only exists to say so in a sentence
        instead of an sqlite constraint message.
        """
        live = self.get_live_for_ticket(profile, ticket_key)
        if live is not None:
            raise StateError(
                f"{ticket_key} already has a live task in profile '{profile}' "
                f"({live.state}), using worktree {live.worktree_path}. Finish it with "
                f"'lazyfish accept' or drop it with 'lazyfish abandon --ticket "
                f"{ticket_key}' before preparing it again."
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

        `notes` is appended, never overwritten (D7). A person who turned an
        earlier plan down wrote their reason into this column, and `status` and
        `show` are the only places anyone ever sees it; replacing it on the
        accept that finally succeeds erased the one record of why the first
        attempt was wrong. Given nothing, the column is left as it is.
        """
        task = self._require(task_id)
        check_transition(task.state, STATE_PLAN_APPROVED)
        merged = _merge_notes(task.notes, notes)
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
                    merged,
                    utc_now(),
                    approved_via,
                    task_id,
                ),
            )
        return self._require(task_id)

    def mark_abandoned(self, task_id: int, *, notes: str | None = None) -> Task:
        task = self._require(task_id)
        check_transition(task.state, STATE_ABANDONED)
        merged = _merge_notes(task.notes, notes)
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
        merged = _merge_notes(task.notes, notes)
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET state = ?, escalation_reason = ?, notes = ? WHERE id = ?",
                (target, escalation_reason, merged, task_id),
            )
        return self._require(task_id)

    def record_drift(self, task_id: int, files: int | None, lines: int | None) -> Task:
        """Store how far the workspace had drifted when the plan was promoted (C1).

        Writes nothing else, and reads nothing to decide with. Kept separate from
        the state transition on purpose: a measurement that shares a code path
        with a decision is one edit away from becoming a condition on it.
        """
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET workspace_delta_files = ?, workspace_delta_lines = ? "
                "WHERE id = ?",
                (files, lines, task_id),
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
                    id, task_id, type, produced_by, call_site, base_commit, parents,
                    promoted_at, promoted_by, attempt, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (task_id, id) DO NOTHING
                """,
                (
                    artifact.id,
                    artifact.task_id,
                    artifact.type,
                    artifact.produced_by,
                    artifact.call_site,
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

        The authority table is consulted here rather than at the call site, and
        that placement is the whole boundary (AC2, AC3). Promotion is the single
        act that makes something the version downstream is built from, so a
        check anywhere else would be one a caller could route around -- and a
        Reviewer promoting an implementation is exactly a caller doing that.
        """
        ensure_authorized(self._require_artifact(task_id, artifact_id))
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
        merged = _merge_notes(task.notes, note)
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

    def get_live(self, profile: str) -> list[Task]:
        """Every task of a profile still inside the window, oldest first (D8).

        The list, not a single row, because a profile may now hold several. A
        command that needs exactly one asks this and refuses to guess when the
        answer has more than one entry (D10) -- the ambiguity is answered where
        the user is, not silently here by taking the newest.
        """
        placeholders = ", ".join("?" for _ in LIVE_STATES)
        rows = self.conn.execute(
            f"SELECT * FROM tasks WHERE profile = ? AND state IN ({placeholders}) ORDER BY id",
            (profile, *LIVE_STATES),
        )
        return [Task.from_row(row) for row in rows]

    def get_live_for_ticket(self, profile: str, ticket_key: str) -> Task | None:
        """The live task for one ticket, if there is one. At most one by index."""
        placeholders = ", ".join("?" for _ in LIVE_STATES)
        row = self.conn.execute(
            f"SELECT * FROM tasks WHERE profile = ? AND ticket_key = ? "
            f"AND state IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            (profile, ticket_key, *LIVE_STATES),
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
                    average_delta_files=_mean(t.workspace_delta_files for t in tasks),
                    average_delta_lines=_mean(t.workspace_delta_lines for t in tasks),
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
