"""The acceptance of LF-5: what survives a stage, and what does not.

The property under test is stated in the plan as `key_property`:

    what the Architect did inside the workspace has no effect on the result of
    promotion or on any later state - except for plan.json.

Note the direction. The superseded revision wanted an out-of-bounds change to be
*refused*; this one wants it *ignored*. Ignoring needs no knowledge of what a
test file is, which is the whole reason the design changed (AC1's note).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lazyfish.artifacts import (
    PROMOTER_HUMAN,
    PROMOTER_ORCHESTRATOR,
    TYPE_REJECTION,
    TYPE_TECHNICAL_PLAN,
)
from lazyfish.cli import cli
from lazyfish.db import (
    APPROVAL_INTERACTIVE,
    APPROVAL_NON_INTERACTIVE,
    STATE_APPROVED,
    STATE_AWAITING_ARTIFACT,
    STATE_COMPLETED,
    STATE_ESCALATED,
    STATE_PROMOTED,
    STATE_REJECTED,
    Database,
)
from lazyfish.orchestrator import (
    BLOCKED_ON_ESCALATION,
    BLOCKED_ON_HUMAN_APPROVAL,
    REASON_STAGE_BUDGET,
    STAGE_ARCHITECT,
)
from lazyfish.rejection import SOURCE_HUMAN, SOURCE_ORCHESTRATOR
from lazyfish.schema import RULE_NEEDS_HUMAN, RULE_REQUIRED_ARRAYS

from .conftest import (
    MODIFIED_TRACKED_FILES,
    FakeTracker,
    artifacts_of,
    commit_everything,
    finish_the_tester_stage,
    git,
    head_commit,
    implement_the_whole_ticket,
    make_plan,
    make_ticket,
    open_database,
    stored_artifact_files,
    stored_content,
    task_of,
    tasks_of,
    write_config,
    write_credentials,
    write_plan,
)

# The three answers to accept's interactive question (D11). They are spelled the
# same as the three non-interactive flags on purpose: one vocabulary for the
# outcome, whether it was typed at a prompt or passed on the command line.
ANSWER_AS_IS = "as-is"
ANSWER_MODIFIED = "modified"
ANSWER_REJECT = "reject"


def artifacts_by_type(env: dict[str, Path], task_id: int, type_name: str) -> list:
    return [item for item in artifacts_of(env, task_id) if item.type == type_name]


def promoted_plan(env: dict[str, Path], task_id: int):
    """The one artifact that made it through the contract, if any."""
    promoted = [
        item
        for item in artifacts_by_type(env, task_id, TYPE_TECHNICAL_PLAN)
        if item.promoted_at is not None
    ]
    assert len(promoted) <= 1, promoted
    return promoted[0] if promoted else None


def rejections(env: dict[str, Path], task_id: int) -> list:
    return artifacts_by_type(env, task_id, TYPE_REJECTION)


def latest_plan_id(env: dict[str, Path], task_id: int) -> str:
    """The plan version a rejection should be pointing at."""
    plans = artifacts_by_type(env, task_id, TYPE_TECHNICAL_PLAN)
    assert plans
    return plans[-1].id


def stat_value(output: str, label: str) -> str | None:
    """The number status printed beside a label, or None if it printed no such line.

    status lays its counts out as a label column and a value (cli._field), so a
    test can ask for one count without pinning the width of the column or the
    order of the lines.
    """
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(label):
            return stripped[len(label) :].strip()
    return None


def rejection_content(env: dict[str, Path], task_id: int, index: int = -1) -> dict:
    found = rejections(env, task_id)
    assert found, "no rejection artifact was recorded"
    artifact = found[index]
    return stored_content(env, task_id, artifact.id)


def set_base_commit(env: dict[str, Path], task_id: int, value: str | None) -> None:
    """Force a task's baseline, including back to NULL as the legacy rows have it."""
    database = Database(env["data"] / "lazyfish.db")
    try:
        database.conn.execute("UPDATE tasks SET base_commit = ? WHERE id = ?", (value, task_id))
        database.conn.commit()
    finally:
        database.close()


@pytest.fixture
def prepared_on_main(
    runner: CliRunner, env: dict[str, Path], repo: Path, tracker: FakeTracker
) -> tuple[dict[str, Path], Path]:
    """A prepared worktree whose profile names its base branch explicitly.

    The baseline tests need a branch that merge-base can actually be asked
    about, which the default configuration leaves implicit.
    """
    write_config(env["config"], profiles={"work": {"repo": str(repo), "base_branch": "main"}})
    write_credentials(env["credentials"])
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    return env, Path(result.stdout.strip().splitlines()[-1][3:])


# --------------------------------------------------------------------------- #
# AC1: the workspace is ignored, not policed
# --------------------------------------------------------------------------- #


def test_a_workspace_that_implemented_the_whole_ticket_still_promotes(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC1, first bullet: those changes are not a violation."""
    env, worktree, _ = prepared_worktree
    implement_the_whole_ticket(worktree)
    write_plan(worktree)

    result = runner.invoke(cli, ["promote"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert task_of(env).state == STATE_PROMOTED


def test_the_promoted_artifact_is_the_plan_and_only_the_plan(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC1, second bullet."""
    env, worktree, _ = prepared_worktree
    created = implement_the_whole_ticket(worktree)
    write_plan(worktree)
    assert runner.invoke(cli, ["promote"]).exit_code == 0

    task = task_of(env)
    artifact = promoted_plan(env, task.id)
    assert artifact is not None
    assert artifact.type == TYPE_TECHNICAL_PLAN

    content = stored_content(env, task.id, artifact.id)
    assert content == make_plan()
    serialised = json.dumps(content, ensure_ascii=False)
    for path in created:
        assert path not in serialised

    assert [path.name for path in stored_artifact_files(env, task.id)] == [artifact.id]


def test_no_command_downstream_of_promotion_sees_the_implementation(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC1, third bullet: this slice's only downstream consumers are show and status."""
    env, worktree, _ = prepared_worktree
    created = implement_the_whole_ticket(worktree)
    write_plan(worktree)
    assert runner.invoke(cli, ["promote"]).exit_code == 0

    for command in (["show"], ["status"]):
        result = runner.invoke(cli, command)
        assert result.exit_code == 0, result.stdout + result.stderr
        for path in created:
            assert path not in result.stdout


def test_promotion_does_not_delete_the_workspace(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """Trap 5 and R3: discarding is about authority, not about rm -rf.

    Deleting on promote would also break LF-1 AC6, where three preps in a row
    must adopt the same worktree.
    """
    env, worktree, _ = prepared_worktree
    created = implement_the_whole_ticket(worktree)
    write_plan(worktree)
    assert runner.invoke(cli, ["promote"]).exit_code == 0

    assert worktree.exists()
    for path in created:
        assert (worktree / path).exists(), path
    for path in MODIFIED_TRACKED_FILES:
        assert (worktree / path).exists(), path
    assert git(worktree, "status", "--porcelain").strip()


def test_the_promoted_content_does_not_change_when_the_workspace_does(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """Authority moves to the artifact at promotion, so later edits are inert."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["promote"]).exit_code == 0

    task = task_of(env)
    artifact = promoted_plan(env, task.id)
    assert artifact is not None
    before = stored_content(env, task.id, artifact.id)

    write_plan(worktree, understanding="rewritten after promotion")
    assert stored_content(env, task.id, artifact.id) == before


# --------------------------------------------------------------------------- #
# AC2: an agent cannot promote its own output
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"needs_human": False}, RULE_NEEDS_HUMAN),
        ({"assumptions": []}, RULE_REQUIRED_ARRAYS),
        ({"alternatives_considered": []}, RULE_REQUIRED_ARRAYS),
        ({"open_questions": [], "needs_human": False}, RULE_REQUIRED_ARRAYS),
    ],
)
def test_a_plan_breaking_an_extra_rule_is_rejected_not_promoted(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker],
    runner: CliRunner,
    overrides: dict,
    rule: str,
) -> None:
    """AC2: each of the three extra rules blocks promotion on its own."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree, **overrides)

    result = runner.invoke(cli, ["promote"])
    assert result.exit_code == 6
    assert rule in result.stdout + result.stderr

    task = task_of(env)
    assert task.state == STATE_REJECTED
    assert promoted_plan(env, task.id) is None
    assert rejections(env, task.id)


def test_a_plan_that_breaks_the_schema_is_rejected(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    env, worktree, _ = prepared_worktree
    write_plan(worktree, changes="not an array")

    assert runner.invoke(cli, ["promote"]).exit_code == 6
    assert promoted_plan(env, task_of(env).id) is None


def test_promote_has_no_flag_that_skips_validation(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC2: the escape hatch belongs to accept, where a person is present.

    A --force on promote would be reachable by anything driving the loop, which
    is exactly the authority the design gives only to the Orchestrator (D2).
    """
    _, worktree, _ = prepared_worktree
    write_plan(worktree, needs_human=False)
    result = runner.invoke(cli, ["promote", "--force"])
    assert result.exit_code == 2
    assert "no such option" in (result.stdout + result.stderr).lower()


def test_accept_cannot_approve_a_plan_that_failed_the_contract(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC2 and AC13: accept promotes first, so a bad plan never reaches approval."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree, needs_human=False)

    result = runner.invoke(cli, ["accept", "--as-is"])
    assert result.exit_code == 6

    task = task_of(env)
    assert task.state != STATE_APPROVED
    assert task.plan_accepted is None
    assert promoted_plan(env, task.id) is None


def test_a_promoted_artifact_records_who_promoted_it(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """The authority table: promotion has exactly one owner."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["promote"]).exit_code == 0

    artifact = promoted_plan(env, task_of(env).id)
    assert artifact is not None
    assert artifact.promoted_by == PROMOTER_ORCHESTRATOR


def test_a_forced_promotion_is_a_human_decision_and_says_so(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """LF-1 R3 kept, and made visible in the data (AC14's method).

    --force is a person overriding the contract at the terminal. It is not a
    path an agent or a runner can take, and the record has to make the override
    legible afterwards rather than looking like a clean promotion.
    """
    env, worktree, _ = prepared_worktree
    write_plan(worktree, needs_human=False)

    result = runner.invoke(cli, ["accept", "--force", "--as-is"])
    assert result.exit_code == 0, result.stdout + result.stderr

    task = task_of(env)
    assert task.plan_accepted is True
    assert "schema bypassed" in (task.notes or "")

    artifact = promoted_plan(env, task.id)
    assert artifact is not None
    assert artifact.promoted_by == PROMOTER_HUMAN


# --------------------------------------------------------------------------- #
# AC3: the rejection is machine readable
# --------------------------------------------------------------------------- #


def test_the_rejection_artifact_carries_the_specified_fields(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC3."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree, needs_human=False)
    assert runner.invoke(cli, ["promote"]).exit_code == 6

    task = task_of(env)
    content = rejection_content(env, task.id)
    assert content["source"] == SOURCE_ORCHESTRATOR
    assert content["target_artifact"]
    assert content["attempt"] == 1
    assert content["route_to"]
    assert content["severity"]
    assert content["required_action"]
    assert [finding["rule"] for finding in content["findings"]] == [RULE_NEEDS_HUMAN]


def test_the_rejection_points_at_the_version_it_judged(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """Lineage: after a retry, the old rejection still names the old artifact."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree, needs_human=False)
    assert runner.invoke(cli, ["promote"]).exit_code == 6
    write_plan(worktree, needs_human=False, understanding="a second, still broken attempt")
    assert runner.invoke(cli, ["promote"]).exit_code == 6

    task = task_of(env)
    recorded = rejections(env, task.id)
    assert len(recorded) == 2
    first = stored_content(env, task.id, recorded[0].id)
    second = stored_content(env, task.id, recorded[1].id)
    assert first["target_artifact"] != second["target_artifact"]
    assert first["attempt"] == 1
    assert second["attempt"] == 2

    plans = {item.id for item in artifacts_by_type(env, task.id, TYPE_TECHNICAL_PLAN)}
    assert {first["target_artifact"], second["target_artifact"]} <= plans


def test_the_rejection_reaches_a_program_through_json(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC3, last sentence."""
    _, worktree, _ = prepared_worktree
    write_plan(worktree, needs_human=False)

    result = runner.invoke(cli, ["promote", "--json"])
    assert result.exit_code == 6
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["artifact"] is None or payload["artifact"]["promoted_at"] is None
    assert payload["rejection"]["findings"][0]["rule"] == RULE_NEEDS_HUMAN


# --------------------------------------------------------------------------- #
# AC4: the baseline comes from the database
# --------------------------------------------------------------------------- #


def test_prep_records_the_baseline(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner, repo: Path
) -> None:
    """D4: the baseline has to exist before anything can be compared against it."""
    env, _, _ = prepared_worktree
    assert task_of(env).base_commit == head_commit(repo)


def test_moving_head_does_not_move_the_baseline(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC4: commit inside the workspace, then promote. Nothing changes."""
    env, worktree, _ = prepared_worktree
    recorded = task_of(env).base_commit
    implement_the_whole_ticket(worktree)
    write_plan(worktree)
    moved = commit_everything(worktree, "the design stage committed its work")

    assert moved != recorded
    assert head_commit(worktree) == moved

    result = runner.invoke(cli, ["promote"])
    assert result.exit_code == 0, result.stdout + result.stderr

    artifact = promoted_plan(env, task_of(env).id)
    assert artifact is not None
    assert artifact.base_commit == recorded
    assert artifact.base_commit != moved


# --------------------------------------------------------------------------- #
# AC11: the rows that predate this column
# --------------------------------------------------------------------------- #


def test_a_legacy_row_derives_its_baseline_and_warns(
    prepared_on_main: tuple[dict[str, Path], Path], runner: CliRunner, repo: Path
) -> None:
    """AC11: derive with merge-base, warn that it was derived, validate anyway."""
    env, worktree = prepared_on_main
    task = task_of(env)
    set_base_commit(env, task.id, None)
    write_plan(worktree)

    result = runner.invoke(cli, ["promote"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "derived" in (result.stdout + result.stderr).lower()

    artifact = promoted_plan(env, task.id)
    assert artifact is not None
    assert artifact.base_commit == head_commit(repo)


def test_a_legacy_row_is_still_validated(
    prepared_on_main: tuple[dict[str, Path], Path], runner: CliRunner
) -> None:
    """AC11: a missing baseline is not a reason to skip the contract.

    The one task actually in flight in the user's database is a legacy row, and
    it is the one that most needs checking.
    """
    env, worktree = prepared_on_main
    task = task_of(env)
    set_base_commit(env, task.id, None)
    write_plan(worktree, needs_human=False)

    assert runner.invoke(cli, ["promote"]).exit_code == 6
    assert promoted_plan(env, task.id) is None
    assert task_of(env).state == STATE_REJECTED


def test_a_baseline_that_cannot_be_derived_is_refused(
    prepared_on_main: tuple[dict[str, Path], Path],
    runner: CliRunner,
    repo: Path,
) -> None:
    """AC11: only a failed derivation refuses, and it says why."""
    env, worktree = prepared_on_main
    task = task_of(env)
    set_base_commit(env, task.id, None)
    write_config(
        env["config"],
        profiles={"work": {"repo": str(repo), "base_branch": "no-such-branch"}},
    )
    write_plan(worktree)

    result = runner.invoke(cli, ["promote"])
    assert result.exit_code != 0
    combined = (result.stdout + result.stderr).lower()
    assert "baseline" in combined or "base_commit" in combined
    assert promoted_plan(env, task.id) is None


# --------------------------------------------------------------------------- #
# AC5: escalation, end to end
# --------------------------------------------------------------------------- #


def test_repeated_rejections_escalate_and_keep_the_last_one(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC5: three failures are retries, the fourth is an escalation."""
    env, worktree, _ = prepared_worktree
    task_id = task_of(env).id

    for attempt in range(1, 4):
        write_plan(worktree, needs_human=False, understanding=f"attempt {attempt}")
        assert runner.invoke(cli, ["promote"]).exit_code == 6
        task = task_of(env)
        assert task.state == STATE_REJECTED, attempt
        assert task.attempt == attempt

    write_plan(worktree, needs_human=False, understanding="attempt 4")
    result = runner.invoke(cli, ["promote"])
    assert result.exit_code != 0

    task = task_of(env)
    assert task.state == STATE_ESCALATED
    assert task.escalation_reason == REASON_STAGE_BUDGET
    assert task.attempt == 4

    last = rejection_content(env, task_id)
    assert last["attempt"] == 4
    assert last["findings"]


def test_an_escalated_ticket_is_not_promoted_again(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """The illegal jump the plan names: ESCALATED straight to promoted."""
    env, worktree, _ = prepared_worktree
    for attempt in range(4):
        write_plan(worktree, needs_human=False, understanding=f"attempt {attempt}")
        runner.invoke(cli, ["promote"])
    assert task_of(env).state == STATE_ESCALATED

    write_plan(worktree)
    result = runner.invoke(cli, ["promote"])
    assert result.exit_code != 0
    assert task_of(env).state == STATE_ESCALATED
    assert promoted_plan(env, task_of(env).id) is None


def test_status_reports_the_tickets_that_stopped(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """The counts the plan asks status to grow: attempts, rejected, escalated."""
    env, worktree, _ = prepared_worktree
    for attempt in range(4):
        write_plan(worktree, needs_human=False, understanding=f"attempt {attempt}")
        runner.invoke(cli, ["promote"])

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "escalated" in result.stdout.lower()


# --------------------------------------------------------------------------- #
# AC13: two steps, in one order
# --------------------------------------------------------------------------- #


def test_promote_stops_at_awaiting_approval(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC13, first bullet: promoted is not approved."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["promote"]).exit_code == 0

    task = task_of(env)
    assert task.state == STATE_PROMOTED
    assert task.state != STATE_APPROVED
    assert task.plan_accepted is None
    assert task.accepted_at is None


def test_approval_after_promotion_reaches_plan_approved(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC13, third bullet."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["promote"]).exit_code == 0

    result = runner.invoke(cli, ["accept", "--as-is"])
    assert result.exit_code == 0, result.stdout + result.stderr

    finish_the_tester_stage(env, runner)
    task = task_of(env)
    assert task.state == STATE_COMPLETED
    assert task.plan_accepted is True


def test_accept_still_works_in_one_step(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """A4: accept promotes internally, so the manual flow is unchanged."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)

    result = runner.invoke(cli, ["accept", "--as-is"])
    assert result.exit_code == 0, result.stdout + result.stderr

    finish_the_tester_stage(env, runner)
    task = task_of(env)
    assert task.state == STATE_COMPLETED
    assert promoted_plan(env, task.id) is not None


def test_promoting_the_same_plan_twice_changes_nothing(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC10, last clause: content addressing makes a repeat promotion idempotent."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["promote"]).exit_code == 0
    task = task_of(env)
    first = promoted_plan(env, task.id)

    assert runner.invoke(cli, ["promote"]).exit_code == 0
    second = promoted_plan(env, task.id)

    assert first is not None and second is not None
    assert first.id == second.id
    assert len(stored_artifact_files(env, task.id)) == 1
    assert task_of(env).state == STATE_PROMOTED


def test_a_rewritten_plan_can_be_promoted_after_a_rejection(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """The retry loop has to actually close, or a rejection is a dead end."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree, needs_human=False)
    assert runner.invoke(cli, ["promote"]).exit_code == 6
    assert task_of(env).state == STATE_REJECTED

    write_plan(worktree)
    assert runner.invoke(cli, ["promote"]).exit_code == 0
    assert task_of(env).state == STATE_PROMOTED
    assert task_of(env).attempt == 2


def test_show_still_works_while_a_plan_waits_for_approval(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """The user reads the plan between promote and accept; that is the whole gate."""
    _, worktree, _ = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["promote"]).exit_code == 0

    result = runner.invoke(cli, ["show"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "PROJ-1" in result.stdout


def test_bare_prep_reprints_one_waiting_ticket_but_an_explicit_ticket_opens_another(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """LF-6 D10: only an explicit ticket request opens concurrent work."""
    env, worktree, tracker = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["promote"]).exit_code == 0

    tracker.tickets = [make_ticket("PROJ-2")]
    repeated = runner.invoke(cli, ["prep"])
    assert len(tasks_of(env)) == 1
    assert "PROJ-1" in repeated.stdout + repeated.stderr
    assert not (worktree.parent / "PROJ-2").exists()

    result = runner.invoke(cli, ["prep", "--ticket", "PROJ-2"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert [task.ticket_key for task in tasks_of(env)] == ["PROJ-1", "PROJ-2"]


def test_an_explicit_ticket_opens_after_a_rejection_without_abandoning_the_first(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    env, worktree, tracker = prepared_worktree
    write_plan(worktree, needs_human=False)
    assert runner.invoke(cli, ["promote"]).exit_code == 6

    tracker.tickets = [make_ticket("PROJ-2")]
    result = runner.invoke(cli, ["prep", "--ticket", "PROJ-2"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert [task.ticket_key for task in tasks_of(env)] == ["PROJ-1", "PROJ-2"]


# --------------------------------------------------------------------------- #
# AC14: an approval that was never answered is visible
# --------------------------------------------------------------------------- #


def test_an_answered_question_is_recorded_as_interactive(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC14, and the measurement the whole slice exists for."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)

    result = runner.invoke(cli, ["accept"], input=f"{ANSWER_AS_IS}\n")
    assert result.exit_code == 0, result.stdout + result.stderr

    task = task_of(env)
    assert task.plan_accepted is True
    assert task.approved_via == APPROVAL_INTERACTIVE


def test_a_non_interactive_approval_is_recorded_as_such(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC14: --as-is skips the five-second question, and the data says so."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["accept", "--as-is"]).exit_code == 0

    task = task_of(env)
    assert task.plan_accepted is True
    assert task.approved_via == APPROVAL_NON_INTERACTIVE


def test_the_two_kinds_of_approval_are_distinguishable(
    runner: CliRunner, env: dict[str, Path], repo: Path, tracker: FakeTracker
) -> None:
    """AC14: one column, two values, no guessing from timestamps."""
    write_config(env["config"], profiles={"work": {"repo": str(repo)}})
    write_credentials(env["credentials"])
    tracker.tickets = [make_ticket("PROJ-1"), make_ticket("PROJ-2")]

    for key, arguments, stdin in (
        ("PROJ-1", ["accept"], f"{ANSWER_AS_IS}\n"),
        ("PROJ-2", ["accept", "--as-is"], None),
    ):
        result = runner.invoke(cli, ["prep", "--ticket", key])
        assert result.exit_code == 0, result.stdout + result.stderr
        worktree = Path(result.stdout.strip().splitlines()[-1][3:])
        write_plan(worktree, ticket=key)
        # Named, because the previous ticket is still live at its tests stage:
        # a ticket no longer leaves the window at `accept` (LF-6 D8, LF-8).
        assert runner.invoke(cli, [*arguments, "--ticket", key], input=stdin).exit_code == 0

    recorded = {task.ticket_key: task.approved_via for task in tasks_of(env)}
    assert recorded == {"PROJ-1": APPROVAL_INTERACTIVE, "PROJ-2": APPROVAL_NON_INTERACTIVE}


def test_a_modified_approval_is_also_non_interactive(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """--modified is the flag form of 'I accepted it, after changing it' (LF-1 AC10)."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    result = runner.invoke(cli, ["accept", "--modified", "--note", "added a migration step"])
    assert result.exit_code == 0, result.stdout + result.stderr

    finish_the_tester_stage(env, runner)
    task = task_of(env)
    assert task.state == STATE_COMPLETED
    assert task.plan_accepted is False
    assert task.notes == "added a migration step"
    assert task.approved_via == APPROVAL_NON_INTERACTIVE


# --------------------------------------------------------------------------- #
# AC15: accept has three exits, and they do not blur into each other
# --------------------------------------------------------------------------- #


def test_the_question_offers_three_outcomes(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """D11: a three-way choice, not a yes/no.

    The shape is the criterion here. A y/n question cannot express the middle
    outcome, and it was a y/n question that put "accepted after changes" and
    "rejected" on the same key in revision 3.
    """
    env, worktree, _ = prepared_worktree
    write_plan(worktree)

    result = runner.invoke(cli, ["accept"], input=f"{ANSWER_AS_IS}\n")
    assert result.exit_code == 0, result.stdout + result.stderr
    for answer in (ANSWER_AS_IS, ANSWER_MODIFIED, ANSWER_REJECT):
        assert answer in result.stdout, f"the question does not offer {answer}"


def test_choosing_as_is_approves_the_plan_unchanged(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC15, first exit."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["accept"], input=f"{ANSWER_AS_IS}\n").exit_code == 0

    finish_the_tester_stage(env, runner)
    task = task_of(env)
    assert task.state == STATE_COMPLETED
    assert task.plan_accepted is True


def test_choosing_modified_approves_the_plan_and_keeps_the_note(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC15, second exit - the bucket LF-1 AC10 measures, reached interactively.

    This is the path revision 3 would have destroyed: the answer that means "I
    accepted it, after changing it" has to keep landing in the approved bucket with
    plan_accepted false, not in the rejection path.
    """
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    result = runner.invoke(cli, ["accept"], input=f"{ANSWER_MODIFIED}\nmissed the rate limiter\n")
    assert result.exit_code == 0, result.stdout + result.stderr

    finish_the_tester_stage(env, runner)
    task = task_of(env)
    assert task.state == STATE_COMPLETED
    assert task.plan_accepted is False
    assert task.notes == "missed the rate limiter"
    assert task.approved_via == APPROVAL_INTERACTIVE
    assert rejections(env, task.id) == [], "accepting after changes is not a rejection"


def test_choosing_reject_sends_the_plan_back_to_the_architect(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC15, third exit. A rejection is not an approval with a caveat."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)

    result = runner.invoke(cli, ["accept"], input=f"{ANSWER_REJECT}\nmissed the rate limiter\n")
    assert result.exit_code == 0, result.stdout + result.stderr

    task = task_of(env)
    assert task.state == STATE_AWAITING_ARTIFACT
    assert task.plan_accepted is None
    assert task.attempt == 1
    assert "missed the rate limiter" in (task.notes or "")


def test_the_reject_flag_is_the_non_interactive_form(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC15: --reject is the new third flag, beside --as-is and --modified."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    result = runner.invoke(cli, ["accept", "--reject", "--note", "missed the rate limiter"])
    assert result.exit_code == 0, result.stdout + result.stderr

    task = task_of(env)
    assert task.state == STATE_AWAITING_ARTIFACT
    assert task.plan_accepted is None
    assert task.attempt == 1
    assert rejection_content(env, task.id)["source"] == SOURCE_HUMAN


@pytest.mark.parametrize(
    "flags",
    [
        ["--as-is", "--modified"],
        ["--as-is", "--reject"],
        ["--modified", "--reject"],
        ["--as-is", "--modified", "--reject"],
    ],
)
def test_the_three_flags_contradict_each_other(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker],
    runner: CliRunner,
    flags: list[str],
) -> None:
    """Three exits, one per run: naming two of them is a mistake, not a priority order."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    result = runner.invoke(cli, ["accept", *flags])
    assert result.exit_code != 0
    assert task_of(env).state == STATE_AWAITING_ARTIFACT


def test_force_cannot_be_combined_with_reject(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """D14: --force belongs to the two accepting exits, not to the refusing one.

    --force means "record it even though validation failed". Turning a plan down
    needs nothing bypassed - the rejection is the point - so the combination has
    no meaning and is a mistake worth naming rather than silently ignoring.

    The plan here fails validation, so this is the case where the two flags would
    actually pull against each other rather than one of them being inert.
    """
    env, worktree, _ = prepared_worktree
    write_plan(worktree, needs_human=False)

    result = runner.invoke(cli, ["accept", "--reject", "--force", "--note", "start again"])
    assert result.exit_code != 0
    assert task_of(env).state == STATE_AWAITING_ARTIFACT
    assert task_of(env).attempt == 0
    assert rejections(env, task_of(env).id) == []


def test_a_human_rejection_is_recorded_as_an_artifact(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC15: the source distinguishes a judgement from a rule violation."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    result = runner.invoke(cli, ["accept"], input=f"{ANSWER_REJECT}\nmissed the rate limiter\n")
    assert result.exit_code == 0, result.stdout + result.stderr

    task = task_of(env)
    content = rejection_content(env, task.id)
    assert content["source"] == SOURCE_HUMAN
    assert content["attempt"] == 1
    assert "missed the rate limiter" in json.dumps(content, ensure_ascii=False)
    assert content["target_artifact"] == latest_plan_id(env, task.id)


def test_the_wording_of_a_human_rejection_is_kept_verbatim(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """The passthrough rule applies to the user's own text as well."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["accept"], input=f"{ANSWER_REJECT}\n漏掉了限流逻辑\n").exit_code == 0

    task = task_of(env)
    assert "漏掉了限流逻辑" in (task.notes or "")
    assert "漏掉了限流逻辑" in json.dumps(rejection_content(env, task.id), ensure_ascii=False)


def test_a_plan_rewritten_after_a_human_rejection_can_be_approved(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC15 closes the loop: back to PLANNING means the ticket continues."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree)
    assert runner.invoke(cli, ["accept"], input=f"{ANSWER_REJECT}\ntoo thin\n").exit_code == 0

    write_plan(worktree, understanding="a second plan, written after the rejection")
    assert runner.invoke(cli, ["accept", "--as-is"]).exit_code == 0

    finish_the_tester_stage(env, runner)
    task = task_of(env)
    assert task.state == STATE_COMPLETED
    assert task.plan_accepted is True
    # One run at the tests stage; the Architect's two were cleared on the move.
    assert task.attempt == 1


def test_status_counts_a_modified_acceptance_apart_from_a_rejection(
    runner: CliRunner, env: dict[str, Path], repo: Path, tracker: FakeTracker
) -> None:
    """AC15, last sentence: the two must not be merged into one number.

    Merging them is the failure revision 3 would have caused silently - the
    middle band of the handoff's judgement table is exactly this count.
    """
    write_config(env["config"], profiles={"work": {"repo": str(repo)}})
    write_credentials(env["credentials"])
    tracker.tickets = [make_ticket(f"PROJ-{index}") for index in (1, 2, 3)]

    for key, arguments in (
        ("PROJ-1", ["accept", "--as-is"]),
        ("PROJ-2", ["accept", "--modified", "--note", "added a migration step"]),
        ("PROJ-3", ["accept", "--reject", "--note", "start again"]),
    ):
        result = runner.invoke(cli, ["prep", "--ticket", key])
        assert result.exit_code == 0, result.stdout + result.stderr
        write_plan(Path(result.stdout.strip().splitlines()[-1][3:]), ticket=key)
        # Named: earlier tickets are still live at their tests stage (LF-8).
        assert runner.invoke(cli, [*arguments, "--ticket", key]).exit_code == 0

    recorded = {task.ticket_key: task.plan_accepted for task in tasks_of(env)}
    assert recorded == {"PROJ-1": True, "PROJ-2": False, "PROJ-3": None}

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert stat_value(result.stdout, "accepted modified") == "1"
    assert stat_value(result.stdout, "rejected") == "1"


# --------------------------------------------------------------------------- #
# AC12: the two commands a runner drives
# --------------------------------------------------------------------------- #


def test_next_in_json_is_only_json(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC12: no table, no prompt, nothing a program has to skip past."""
    _, worktree, _ = prepared_worktree
    result = runner.invoke(cli, ["next", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr

    payload = json.loads(result.stdout)
    assert payload["stage"] == STAGE_ARCHITECT
    assert payload["workspace"] == str(worktree)
    assert payload["consumes"] == []
    assert payload["blocked_on"] is None


def test_promote_in_json_is_only_json(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    env, worktree, _ = prepared_worktree
    write_plan(worktree)

    result = runner.invoke(cli, ["promote", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr

    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["state"] == STATE_PROMOTED
    assert payload["rejection"] is None
    assert payload["artifact"]["id"] == promoted_plan(env, task_of(env).id).id
    assert payload["artifact"]["type"] == TYPE_TECHNICAL_PLAN
    assert payload["artifact"]["promoted_at"]


def test_a_runner_can_drive_one_stage_with_json_alone(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC12 and the integration case: a script standing in for lazyfish-runner.

    It never reads a human-readable line, and it stops of its own accord at the
    approval gate, because that is a state and not a message (D10).
    """
    env, _, _ = prepared_worktree

    step = json.loads(runner.invoke(cli, ["next", "--json"]).stdout)
    assert step["stage"] == STAGE_ARCHITECT
    workspace = Path(step["workspace"])

    write_plan(workspace)  # stands in for the agent run

    outcome = json.loads(runner.invoke(cli, ["promote", "--json"]).stdout)
    assert outcome["ok"] is True

    step = json.loads(runner.invoke(cli, ["next", "--json"]).stdout)
    assert step["stage"] is None
    assert step["blocked_on"] == BLOCKED_ON_HUMAN_APPROVAL
    assert step["state"] == STATE_PROMOTED


def test_a_runner_is_told_when_a_ticket_escalated(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """The runner's loop has one stop condition with two reasons (section 8)."""
    env, worktree, _ = prepared_worktree
    for attempt in range(4):
        write_plan(worktree, needs_human=False, understanding=f"attempt {attempt}")
        runner.invoke(cli, ["promote", "--json"])

    step = json.loads(runner.invoke(cli, ["next", "--json"]).stdout)
    assert step["stage"] is None
    assert step["blocked_on"] == BLOCKED_ON_ESCALATION
    assert step["state"] == STATE_ESCALATED


def test_promote_without_a_plan_says_where_to_put_one(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    _, _, _ = prepared_worktree
    result = runner.invoke(cli, ["promote"])
    assert result.exit_code != 0
    assert "plan.json" in result.stdout + result.stderr


def test_next_without_json_is_readable(
    prepared_worktree: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """The same answer for a person: what to do next, and where."""
    _, worktree, _ = prepared_worktree
    result = runner.invoke(cli, ["next"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert str(worktree) in result.stdout
    assert STAGE_ARCHITECT.lower() in result.stdout.lower()


def test_next_without_a_prepared_ticket_is_still_answerable(
    runner: CliRunner, configured: dict[str, Path]
) -> None:
    result = runner.invoke(cli, ["next", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["stage"] is None


# --------------------------------------------------------------------------- #
# AC6: the idempotent path is unchanged
# --------------------------------------------------------------------------- #


def test_three_preps_still_record_one_baseline(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker, repo: Path
) -> None:
    """AC6: LF-1's idempotence, extended to the column this slice adds."""
    first = runner.invoke(cli, ["prep"])
    second = runner.invoke(cli, ["prep"])
    third = runner.invoke(cli, ["prep"])

    assert first.stdout == second.stdout == third.stdout
    rows = tasks_of(configured)
    assert len(rows) == 1
    assert rows[0].base_commit == head_commit(repo)


def test_accept_is_refused_at_a_stage_with_no_human_gate(
    prepared_worktree: tuple[dict[str, Path], Path, object],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: approving where nobody approves would be recorded in silence.

    The row it would leave -- APPROVED, plan_accepted set, at a stage with no
    gate -- is indistinguishable from a real approval in the as-is rate, and no
    rule anywhere is broken by it. That is why the check exists (LF-7 D4).
    """
    env, worktree, _ = prepared_worktree
    write_plan(worktree)

    with open_database(env) as database:
        task = database.get_live("work")[0]
        database.conn.execute("UPDATE tasks SET current_stage = 'coder' WHERE id = ?", (task.id,))
        database.conn.commit()

    result = runner.invoke(cli, ["accept", "--as-is"])
    assert result.exit_code != 0
    assert "coder" in result.output
    assert "promote" in result.output

    with open_database(env) as database:
        unchanged = database.get(task.id)
    assert unchanged is not None
    assert unchanged.plan_accepted is None
    assert unchanged.accepted_at is None


def test_the_attempt_count_survives_completion(
    prepared_worktree: tuple[dict[str, Path], Path, object], runner: CliRunner
) -> None:
    """AC6's other side: clearing `attempt` at the end erases what it measured."""
    env, worktree, _ = prepared_worktree
    write_plan(worktree, assumptions=[])
    assert runner.invoke(cli, ["promote"]).exit_code != 0

    write_plan(worktree)
    assert runner.invoke(cli, ["accept", "--as-is"]).exit_code == 0

    finish_the_tester_stage(env, runner)
    task = task_of(env)
    assert task.state == STATE_COMPLETED
    # `attempt` belongs to the stage the ticket finished at, and the tests stage
    # took one run. The Architect's two were cleared when the stage moved --
    # that is the clearing LF-7 D5 is about. What must not be cleared is this
    # count at the end of the sequence, which is what the assertion pins.
    assert task.attempt == 1
    assert task.ticket_attempts == 3
