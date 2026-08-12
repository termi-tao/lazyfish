"""State storage: legal transitions, one task in flight, statistics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lazyfish.db import (
    STATE_ABANDONED,
    STATE_PLAN_APPROVED,
    STATE_READY_FOR_PLAN,
    Database,
    check_transition,
)
from lazyfish.errors import StateError


@pytest.fixture
def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "lazyfish.db")
    instance.initialise()
    return instance


def add(database: Database, key: str = "PROJ-1", profile: str = "default", top: bool = True):
    return database.insert_task(
        ticket_key=key,
        ticket_title=f"title for {key}",
        repo_profile=profile,
        branch=f"lazyfish/{key}",
        worktree_path=f"/tmp/worktrees/{profile}/{key}",
        artifacts_path=f"/tmp/worktrees/{profile}/{key}/artifacts/{key}",
        was_top_pick=top,
    )


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
    assert database.get_in_flight("default").id == task.id
    assert database.get_by_ticket("PROJ-1", "default").id == task.id


def test_one_task_in_flight_per_profile(database: Database) -> None:
    add(database, "PROJ-1")
    with pytest.raises(StateError, match="already waiting for a plan for PROJ-1"):
        add(database, "PROJ-2")


def test_profiles_do_not_interfere(database: Database) -> None:
    first = add(database, "PROJ-1", profile="default")
    second = add(database, "WEB-9", profile="frontend")
    assert database.get_in_flight("default").id == first.id
    assert database.get_in_flight("frontend").id == second.id


def test_accept_records_the_outcome(database: Database) -> None:
    task = add(database)
    accepted = database.mark_accepted(task.id, plan_accepted=False, notes="missed a case")
    assert accepted.state == STATE_PLAN_APPROVED
    assert accepted.plan_accepted is False
    assert accepted.notes == "missed a case"
    assert accepted.accepted_at is not None


def test_abandon_frees_the_profile(database: Database) -> None:
    task = add(database, "PROJ-1")
    database.mark_abandoned(task.id, notes="deprioritised")
    assert database.get_in_flight("default") is None
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
    database.mark_accepted(task.id, plan_accepted=True)
    with pytest.raises(StateError, match="Cannot move a task"):
        database.mark_accepted(task.id, plan_accepted=True)


def test_check_transition_table() -> None:
    check_transition(STATE_READY_FOR_PLAN, STATE_PLAN_APPROVED)
    check_transition(STATE_READY_FOR_PLAN, STATE_ABANDONED)
    check_transition(STATE_PLAN_APPROVED, STATE_ABANDONED)
    with pytest.raises(StateError):
        check_transition(STATE_ABANDONED, STATE_PLAN_APPROVED)
    with pytest.raises(StateError):
        check_transition(STATE_PLAN_APPROVED, STATE_READY_FOR_PLAN)


def test_append_note_keeps_the_existing_text(database: Database) -> None:
    task = add(database)
    database.mark_accepted(task.id, plan_accepted=False, notes="first")
    updated = database.append_note(task.id, "schema bypassed")
    assert updated.notes == "first\nschema bypassed"


def test_stats_group_by_profile(database: Database) -> None:
    first = add(database, "PROJ-1", profile="default")
    database.mark_accepted(first.id, plan_accepted=True)
    second = add(database, "PROJ-2", profile="default")
    database.mark_accepted(second.id, plan_accepted=False, notes="changed")
    third = add(database, "WEB-1", profile="frontend", top=False)
    database.mark_abandoned(third.id)

    grouped = {stats.repo_profile: stats for stats in database.stats()}
    assert set(grouped) == {"default", "frontend"}
    assert grouped["default"].accepted_as_is == 1
    assert grouped["default"].accepted_modified == 1
    assert grouped["default"].as_is_rate == pytest.approx(0.5)
    assert grouped["frontend"].abandoned == 1
    assert grouped["frontend"].top_pick_count == 0

    only_default = database.stats("default")
    assert len(only_default) == 1
    assert only_default[0].prepared == 2


def test_average_minutes_to_accept(database: Database) -> None:
    task = add(database)
    accepted = database.mark_accepted(task.id, plan_accepted=True)

    started = datetime.fromisoformat(accepted.prepared_at) - timedelta(minutes=30)
    database.conn.execute(
        "UPDATE tasks SET prepared_at = ? WHERE id = ?",
        (started.isoformat(timespec="seconds"), task.id),
    )
    database.conn.commit()

    stats = database.stats("default")[0]
    assert stats.average_minutes_to_accept == pytest.approx(30.0, abs=0.5)


def test_timestamps_are_utc(database: Database) -> None:
    task = add(database)
    parsed = datetime.fromisoformat(task.prepared_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == UTC.utcoffset(None)
