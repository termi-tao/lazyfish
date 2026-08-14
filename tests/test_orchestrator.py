"""The Orchestrator: the deterministic control layer.

Everything here is called as a plain function. That is the point of AC9 and of
R4: if these decisions cannot be reached without a click Context, they are still
living in cli.py and the second stage will have to re-do this work.

The retry budget is not bookkeeping. D1 accepts that promotion is an after-the
-fact judgement, which means a runaway agent is stopped by the budget and by
nothing else, so both levels are pinned here at their boundary values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lazyfish.db import (
    STATE_ABANDONED,
    STATE_ESCALATED,
    STATE_PLAN_APPROVED,
    STATE_PLAN_PROMOTED,
    STATE_READY_FOR_PLAN,
    STATE_REJECTED,
    Database,
)
from lazyfish.errors import StateError
from lazyfish.orchestrator import (
    BLOCKED_ON_ESCALATION,
    BLOCKED_ON_HUMAN_APPROVAL,
    PER_STAGE_BUDGET,
    PER_TICKET_BUDGET,
    REASON_STAGE_BUDGET,
    REASON_TICKET_BUDGET,
    STAGE_ARCHITECT,
    budget_exceeded,
    decide_promotion,
    ensure_promotable,
    next_step,
    route_for,
)
from lazyfish.rejection import (
    ROUTE_ARCHITECT,
    ROUTE_HUMAN,
    RULE_HUMAN_JUDGEMENT,
    SOURCE_HUMAN,
    SOURCE_ORCHESTRATOR,
    Finding,
    RejectionArtifact,
    from_human,
    from_plan_issues,
)
from lazyfish.schema import (
    RULE_FILES_EXIST,
    RULE_NEEDS_HUMAN,
    RULE_REQUIRED_ARRAYS,
    RULE_SCHEMA,
    PlanIssue,
    validate_plan,
)

from .conftest import make_plan

VALIDATOR_RULES = frozenset({RULE_SCHEMA, RULE_REQUIRED_ARRAYS, RULE_NEEDS_HUMAN, RULE_FILES_EXIST})

AN_ARTIFACT_ID = "a" * 64


def issue(rule: str = RULE_NEEDS_HUMAN) -> PlanIssue:
    return PlanIssue(rule, "plan.needs_human", "must be true")


@pytest.fixture
def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "lazyfish.db")
    instance.initialise()
    return instance


def a_task(database: Database, *, state: str = STATE_READY_FOR_PLAN, **columns: object):
    """A task row forced into a given state, without running the flow to reach it.

    R5 asks for the per-ticket budget to be verified with a constructed state
    rather than by actually running an agent twelve times.
    """
    task = database.insert_task(
        ticket_key="PROJ-1",
        ticket_title="Password reset links expire too early",
        profile="work",
        branch="lazyfish/PROJ-1",
        worktree_path="/tmp/worktrees/work/PROJ-1/architect",
        artifacts_path="/tmp/worktrees/work/PROJ-1/architect/artifacts/PROJ-1",
        was_top_pick=True,
        base_commit="0" * 40,
    )
    columns = {"state": state, **columns}
    assignments = ", ".join(f"{name} = ?" for name in columns)
    database.conn.execute(
        f"UPDATE tasks SET {assignments} WHERE id = ?", (*columns.values(), task.id)
    )
    database.conn.commit()
    return database.get(task.id)


# --------------------------------------------------------------------------- #
# AC5: two budgets, one unit
# --------------------------------------------------------------------------- #


def test_the_budgets_are_the_specified_defaults() -> None:
    """D6: three per stage, twelve per ticket, counted in agent runs."""
    assert PER_STAGE_BUDGET == 3
    assert PER_TICKET_BUDGET == 12


def test_a_run_within_both_budgets_is_not_over() -> None:
    assert budget_exceeded(stage_attempts=1, ticket_attempts=1) is None
    assert budget_exceeded(stage_attempts=3, ticket_attempts=3) is None


def test_the_fourth_run_of_a_stage_is_over_budget() -> None:
    """AC5, first half: the boundary is 3/4."""
    assert budget_exceeded(stage_attempts=4, ticket_attempts=4) == REASON_STAGE_BUDGET


def test_the_ticket_budget_bites_even_when_the_stage_budget_does_not() -> None:
    """AC5, second half: 12/13, with the stage well inside its own budget (R5)."""
    assert budget_exceeded(stage_attempts=2, ticket_attempts=12) is None
    assert budget_exceeded(stage_attempts=2, ticket_attempts=13) == REASON_TICKET_BUDGET


def test_the_two_escalation_reasons_are_distinguishable() -> None:
    """AC5: 'the stage gave up' and 'the ticket gave up' are different facts."""
    assert REASON_STAGE_BUDGET != REASON_TICKET_BUDGET
    assert REASON_STAGE_BUDGET and REASON_TICKET_BUDGET


def test_waiting_for_a_human_is_not_an_escalation_reason() -> None:
    """AC5's note: an approval gate is a standing door, not an exception."""
    assert STATE_ESCALATED != STATE_PLAN_PROMOTED
    assert REASON_STAGE_BUDGET != STATE_PLAN_PROMOTED
    assert REASON_TICKET_BUDGET != STATE_PLAN_PROMOTED
    assert BLOCKED_ON_HUMAN_APPROVAL != BLOCKED_ON_ESCALATION


# --------------------------------------------------------------------------- #
# The promotion decision
# --------------------------------------------------------------------------- #


def test_a_clean_plan_is_promoted() -> None:
    decision = decide_promotion(
        artifact_id=AN_ARTIFACT_ID, issues=[], stage_attempts=1, ticket_attempts=1
    )
    assert decision.promoted is True
    assert decision.next_state == STATE_PLAN_PROMOTED
    assert decision.rejection is None
    assert decision.escalation_reason is None


def test_a_clean_plan_is_promoted_even_late_in_the_budget() -> None:
    """The budget stops retries, not a result that finally passes."""
    decision = decide_promotion(
        artifact_id=AN_ARTIFACT_ID, issues=[], stage_attempts=9, ticket_attempts=20
    )
    assert decision.promoted is True
    assert decision.next_state == STATE_PLAN_PROMOTED


def test_a_failing_plan_is_rejected_and_routed_back() -> None:
    decision = decide_promotion(
        artifact_id=AN_ARTIFACT_ID, issues=[issue()], stage_attempts=1, ticket_attempts=1
    )
    assert decision.promoted is False
    assert decision.next_state == STATE_REJECTED
    assert decision.escalation_reason is None
    assert decision.rejection is not None
    assert decision.rejection.route_to == ROUTE_ARCHITECT
    assert decision.rejection.attempt == 1
    assert decision.rejection.target_artifact == AN_ARTIFACT_ID


def test_a_failing_plan_over_the_stage_budget_escalates() -> None:
    """AC5: escalation carries the last rejection with it."""
    decision = decide_promotion(
        artifact_id=AN_ARTIFACT_ID, issues=[issue()], stage_attempts=4, ticket_attempts=4
    )
    assert decision.promoted is False
    assert decision.next_state == STATE_ESCALATED
    assert decision.escalation_reason == REASON_STAGE_BUDGET
    assert decision.rejection is not None
    assert decision.rejection.findings


def test_a_failing_plan_over_the_ticket_budget_escalates_for_its_own_reason() -> None:
    decision = decide_promotion(
        artifact_id=AN_ARTIFACT_ID, issues=[issue()], stage_attempts=2, ticket_attempts=13
    )
    assert decision.next_state == STATE_ESCALATED
    assert decision.escalation_reason == REASON_TICKET_BUDGET


def test_the_decision_needs_no_click_context() -> None:
    """AC9 and R4, stated as a test rather than as an intention.

    Every other test in this module already calls the decisions directly; this
    one names the criterion, so that a failure reads as 'the Orchestrator became
    a shell around the CLI' rather than as an unrelated import error.
    """
    import lazyfish.orchestrator as orchestrator

    source = Path(orchestrator.__file__).read_text(encoding="utf-8")
    assert "import click" not in source
    assert "from click" not in source


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def test_a_contract_violation_goes_back_to_the_layer_that_produced_it() -> None:
    """S4: schema violation -> same layer."""
    assert route_for([Finding(RULE_SCHEMA, "plan.changes: required")]) == ROUTE_ARCHITECT


def test_the_same_rule_failing_twice_in_a_row_goes_to_a_human() -> None:
    """S4: two consecutive failures of the same rule at the same layer escalate."""
    previous = [Finding(RULE_NEEDS_HUMAN, "first attempt")]
    again = [Finding(RULE_NEEDS_HUMAN, "second attempt")]
    assert route_for(again, previous_findings=previous) == ROUTE_HUMAN


def test_a_different_rule_the_second_time_still_goes_back_to_the_layer() -> None:
    previous = [Finding(RULE_NEEDS_HUMAN, "first attempt")]
    again = [Finding(RULE_FILES_EXIST, "second attempt")]
    assert route_for(again, previous_findings=previous) == ROUTE_ARCHITECT


def test_routing_is_deterministic_for_the_same_input() -> None:
    findings = [Finding(RULE_SCHEMA, "plan.changes: required")]
    assert route_for(findings) == route_for(findings)


# --------------------------------------------------------------------------- #
# AC3: the rejection is data, not prose
# --------------------------------------------------------------------------- #


def test_a_rejection_carries_every_specified_field() -> None:
    """D5, field for field."""
    rejection = from_plan_issues([issue()], target_artifact=AN_ARTIFACT_ID, attempt=1)
    for name in (
        "target_artifact",
        "source",
        "severity",
        "findings",
        "attempt",
        "required_action",
        "route_to",
    ):
        assert hasattr(rejection, name), name
    assert rejection.source == SOURCE_ORCHESTRATOR
    assert rejection.required_action


def test_every_finding_rule_comes_from_the_validator() -> None:
    """AC3: rule ids, never free text.

    A rejection whose findings are prose cannot be routed, counted, or compared
    with the previous attempt, which is what the routing rule above needs.
    """
    plan = make_plan(needs_human=False, assumptions=[], open_questions=[])
    issues = validate_plan(plan)
    assert issues

    rejection = from_plan_issues(issues, target_artifact=AN_ARTIFACT_ID, attempt=1)
    assert rejection.findings
    for finding in rejection.findings:
        assert finding.rule in VALIDATOR_RULES
        assert finding.evidence


def test_a_rejection_keeps_one_finding_per_issue() -> None:
    issues = [issue(RULE_NEEDS_HUMAN), issue(RULE_REQUIRED_ARRAYS)]
    rejection = from_plan_issues(issues, target_artifact=AN_ARTIFACT_ID, attempt=2)
    assert [finding.rule for finding in rejection.findings] == [
        RULE_NEEDS_HUMAN,
        RULE_REQUIRED_ARRAYS,
    ]
    assert rejection.attempt == 2


def test_a_rejection_serialises_to_json_safe_data() -> None:
    """AC3: --json output has to be consumable by a program."""
    import json

    rejection = from_plan_issues([issue()], target_artifact=AN_ARTIFACT_ID, attempt=1)
    payload = rejection.to_dict()
    round_tripped = json.loads(json.dumps(payload, ensure_ascii=False))

    assert round_tripped["target_artifact"] == AN_ARTIFACT_ID
    assert round_tripped["source"] == SOURCE_ORCHESTRATOR
    assert round_tripped["route_to"] == ROUTE_ARCHITECT
    assert round_tripped["attempt"] == 1
    assert round_tripped["findings"][0]["rule"] == RULE_NEEDS_HUMAN
    assert "evidence" in round_tripped["findings"][0]


def test_a_human_rejection_is_marked_as_one() -> None:
    """AC15: a judgement rejection and a rule rejection are different sources."""
    rejection = from_human(
        "the plan misses the rate limiter", target_artifact=AN_ARTIFACT_ID, attempt=1
    )
    assert rejection.source == SOURCE_HUMAN
    assert rejection.route_to == ROUTE_ARCHITECT
    assert [finding.rule for finding in rejection.findings] == [RULE_HUMAN_JUDGEMENT]
    assert rejection.findings[0].evidence == "the plan misses the rate limiter"


def test_a_human_rejection_keeps_the_wording_verbatim() -> None:
    """The passthrough rule: the note is the user's text, in the user's language."""
    note = "漏掉了限流逻辑"
    rejection = from_human(note, target_artifact=AN_ARTIFACT_ID, attempt=1)
    assert rejection.findings[0].evidence == note
    assert note in str(rejection.to_dict())


def test_the_human_rule_id_is_not_one_of_the_validator_rules() -> None:
    """A judgement is not a rule violation; conflating them corrupts the counts."""
    assert RULE_HUMAN_JUDGEMENT not in VALIDATOR_RULES


def test_a_rejection_is_immutable() -> None:
    """A record of what was decided, not a scratch object to edit later."""
    rejection = from_plan_issues([issue()], target_artifact=AN_ARTIFACT_ID, attempt=1)
    with pytest.raises(AttributeError):
        rejection.attempt = 5  # type: ignore[misc]


def test_a_rejection_names_the_artifact_it_rejected_not_the_task() -> None:
    """Lineage: a retry produces a new artifact, and the old rejection still
    points at the version it actually judged."""
    first = from_plan_issues([issue()], target_artifact="a" * 64, attempt=1)
    second = from_plan_issues([issue()], target_artifact="b" * 64, attempt=2)
    assert first.target_artifact != second.target_artifact
    assert isinstance(first, RejectionArtifact)


# --------------------------------------------------------------------------- #
# AC13: promotable states
# --------------------------------------------------------------------------- #


def test_a_prepared_task_can_be_promoted() -> None:
    ensure_promotable(STATE_READY_FOR_PLAN)


def test_a_rejected_task_can_be_promoted_again() -> None:
    """The retry loop: a rejection is not the end of the ticket."""
    ensure_promotable(STATE_REJECTED)


@pytest.mark.parametrize(
    "state",
    [STATE_ESCALATED, STATE_PLAN_APPROVED, STATE_ABANDONED],
)
def test_states_that_cannot_be_promoted_say_so(state: str) -> None:
    with pytest.raises(StateError):
        ensure_promotable(state)


# --------------------------------------------------------------------------- #
# AC12: what `next` tells a runner
# --------------------------------------------------------------------------- #


def test_next_step_on_a_prepared_task_names_the_stage_and_the_workspace(
    database: Database,
) -> None:
    step = next_step(a_task(database))
    assert step.stage == STAGE_ARCHITECT
    assert step.workspace == "/tmp/worktrees/work/PROJ-1/architect"
    assert step.state == STATE_READY_FOR_PLAN
    assert step.blocked_on is None


def test_the_architect_is_told_it_consumes_no_artifacts(database: Database) -> None:
    """The authority table: Architect consumes the Ticket, which is not an artifact."""
    assert next_step(a_task(database)).consumes == ()


def test_a_rejected_task_is_sent_round_again(database: Database) -> None:
    step = next_step(a_task(database, state=STATE_REJECTED, attempt=1))
    assert step.stage == STAGE_ARCHITECT
    assert step.attempt == 1
    assert step.blocked_on is None


def test_a_promoted_plan_stops_the_loop_for_approval(database: Database) -> None:
    """D10: PLAN_PROMOTED is a terminal state for a runner, by construction."""
    step = next_step(a_task(database, state=STATE_PLAN_PROMOTED))
    assert step.stage is None
    assert step.blocked_on == BLOCKED_ON_HUMAN_APPROVAL


def test_an_escalated_task_stops_the_loop_for_a_different_reason(database: Database) -> None:
    step = next_step(a_task(database, state=STATE_ESCALATED, escalation_reason=REASON_STAGE_BUDGET))
    assert step.stage is None
    assert step.blocked_on == BLOCKED_ON_ESCALATION


@pytest.mark.parametrize("state", [STATE_PLAN_APPROVED, STATE_ABANDONED])
def test_a_finished_task_has_no_next_stage(database: Database, state: str) -> None:
    step = next_step(a_task(database, state=state))
    assert step.stage is None
    assert step.blocked_on is None


def test_next_step_without_a_task_is_still_answerable() -> None:
    step = next_step(None)
    assert step.stage is None
    assert step.state is None


def test_next_step_serialises_for_the_json_output(database: Database) -> None:
    """AC12: these are the fields a runner drives the loop with."""
    import json

    payload = json.loads(json.dumps(next_step(a_task(database)).to_dict()))
    for key in ("state", "stage", "workspace", "consumes", "blocked_on"):
        assert key in payload, key


# --------------------------------------------------------------------------- #
# The Orchestrator is not an agent (A-4)
# --------------------------------------------------------------------------- #


def test_the_orchestrator_reaches_no_model() -> None:
    """AC16 in spirit: the new module is inside the invariant, not beside it."""
    import lazyfish.orchestrator as orchestrator

    from .test_invariants import find_ai_subprocess_calls, find_llm_imports

    files = [Path(orchestrator.__file__)]
    assert find_ai_subprocess_calls(files) == []
    assert find_llm_imports(files) == []


def test_the_same_decision_twice_gives_the_same_answer() -> None:
    """Determinism is the property that makes the whole layer testable (section 10)."""
    first = decide_promotion(
        artifact_id=AN_ARTIFACT_ID, issues=[issue()], stage_attempts=2, ticket_attempts=2
    )
    second = decide_promotion(
        artifact_id=AN_ARTIFACT_ID, issues=[issue()], stage_attempts=2, ticket_attempts=2
    )
    assert first == second


def test_a_constructed_state_is_really_stored(database: Database) -> None:
    """The helper above writes the state directly; the columns must accept it."""
    task = a_task(database, state=STATE_ESCALATED, attempt=4, ticket_attempts=4)
    assert task.state == STATE_ESCALATED
    assert task.attempt == 4
    assert task.ticket_attempts == 4
