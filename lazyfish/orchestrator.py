"""The Orchestrator: the deterministic control layer.

Not a fifth agent, and the terminology matters. This is the one part of the
system that is guaranteed deterministic, and calling it an agent invites someone
to implement it with a model (A-4). It imports no LLM SDK and starts no process;
agent invocation lives in a separate package, behind an adapter.

What lives here is judgement that must be reproducible:

  budget_exceeded    has this stage, or this ticket, run out of attempts
  decide_promotion   given the contract's verdict, what happens next
  route_for          which layer a rejection goes back to
  ensure_promotable  is this state one a promotion can even be attempted from
  next_step          what a driver should do next, and what stops it

Two properties are deliberate. First, everything here is a plain function over
plain values: no click Context, no I/O beyond reading a task record. That is
AC9's criterion and R4's mitigation -- if these decisions could only be reached
through the CLI, they would still be living in cli.py and the second stage would
have to move them again. Second, the state transition *legality* table is not
here; it stays in `db.py`, next to the schema that enforces the same invariants
structurally. This module decides what should happen, and `db.py` refuses
anything that should not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .db import (
    STATE_ABANDONED,
    STATE_ESCALATED,
    STATE_PLAN_APPROVED,
    STATE_PLAN_PROMOTED,
    STATE_READY_FOR_PLAN,
    STATE_REJECTED,
)
from .errors import StateError
from .rejection import (
    ACTION_HUMAN_REVIEW,
    ACTION_REVISE_PLAN,
    ROUTE_ARCHITECT,
    ROUTE_HUMAN,
    Finding,
    RejectionArtifact,
    from_plan_issues,
)
from .schema import PlanIssue

# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #

STAGE_ARCHITECT = "architect"
"""The only stage implemented this slice. The others arrive with their contracts."""


# --------------------------------------------------------------------------- #
# Retry budgets (D6)
# --------------------------------------------------------------------------- #

PER_STAGE_BUDGET = 3
PER_TICKET_BUDGET = 12
"""Two levels, both counted in agent runs.

The unit is runs rather than wall clock or tokens: countable, measurable, and
independent of any vendor's pricing. The per-ticket ceiling is not redundant
with the per-stage one -- four stages retrying three times each is twelve runs,
and with no total ceiling a single pathological ticket can eat a week of quota.

This is not bookkeeping. D1 accepted that promotion is an after-the-fact
judgement, which means the thing that stops a runaway agent is the budget and
nothing else. It replaced the lock the superseded revision would have used.
"""

REASON_STAGE_BUDGET = "stage-budget-exhausted"
REASON_TICKET_BUDGET = "ticket-budget-exhausted"
"""Why a ticket escalated. Kept distinct because "this stage gave up" and "this
ticket gave up" are different facts and lead to different human responses."""


def budget_exceeded(*, stage_attempts: int, ticket_attempts: int) -> str | None:
    """The escalation reason if either budget is spent, otherwise None.

    The boundaries are inclusive: the third attempt at a stage is still within
    budget, the fourth is not. The stage limit is reported in preference to the
    ticket limit when both are spent, because it is the more specific fact.
    """
    if stage_attempts > PER_STAGE_BUDGET:
        return REASON_STAGE_BUDGET
    if ticket_attempts > PER_TICKET_BUDGET:
        return REASON_TICKET_BUDGET
    return None


# --------------------------------------------------------------------------- #
# Routing (S4)
# --------------------------------------------------------------------------- #


def route_for(
    findings: Sequence[Finding],
    previous_findings: Sequence[Finding] | None = None,
) -> str:
    """Where a rejection goes back to.

    Two deterministic rules this slice:

    - A contract violation goes back to the layer that produced the artifact.
      The Architect wrote a plan that does not satisfy the contract, so the
      Architect is who can fix it.
    - The same rule failing twice in a row goes to a person. A layer that could
      fix a problem would have fixed it the second time; repeating the same
      failure is evidence the instruction, not the attempt, is wrong.

    Comparison is on rule ids, which is the whole reason findings carry ids
    rather than prose (D5): "the same reason as last time" is not answerable
    over free text.
    """
    if previous_findings:
        repeated = {finding.rule for finding in findings} & {
            finding.rule for finding in previous_findings
        }
        if repeated:
            return ROUTE_HUMAN
    return ROUTE_ARCHITECT


# --------------------------------------------------------------------------- #
# Promotable states (AC13)
# --------------------------------------------------------------------------- #

PROMOTABLE_STATES = frozenset({STATE_READY_FOR_PLAN, STATE_REJECTED})
"""States a promotion may be attempted from.

`REJECTED` is in the set because a rejection is not the end of a ticket -- it is
the retry loop. `PLAN_PROMOTED` is not: promoting twice would mean the approval
gate can be re-entered from underneath, and `PLAN_APPROVED` is not either,
because the order of promote and approve is fixed (AC13).
"""


def ensure_promotable(state: str) -> None:
    """Raise unless a promotion may be attempted from `state`."""
    if state in PROMOTABLE_STATES:
        return
    if state == STATE_PLAN_PROMOTED:
        raise StateError(
            "This plan has already been promoted and is waiting for approval.\n"
            "Run 'lazyfish show' to read it, then 'lazyfish accept'."
        )
    raise StateError(
        f"A plan cannot be promoted from state {state}. "
        f"Promotion is only possible from {' or '.join(sorted(PROMOTABLE_STATES))}."
    )


# --------------------------------------------------------------------------- #
# The promotion decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PromotionDecision:
    """What the Orchestrator concluded, as a value.

    Returned rather than applied, so that the decision can be asserted on its
    own and the caller stays responsible for persistence. Frozen and comparable,
    which is how "the same inputs give the same answer" is stated as a test.
    """

    promoted: bool
    next_state: str
    rejection: RejectionArtifact | None = None
    escalation_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe shape for `promote --json` (AC12)."""
        return {
            "promoted": self.promoted,
            "next_state": self.next_state,
            "escalation_reason": self.escalation_reason,
            "rejection": self.rejection.to_dict() if self.rejection else None,
        }


def decide_promotion(
    *,
    artifact_id: str,
    issues: Sequence[PlanIssue],
    stage_attempts: int,
    ticket_attempts: int,
    previous_findings: Sequence[Finding] | None = None,
) -> PromotionDecision:
    """Judge one promotion attempt.

    The order of the two checks is the point. A plan that satisfies the contract
    is promoted regardless of how much budget has been spent: the budget exists
    to stop retrying, not to reject a result that finally passed. Only once the
    contract has failed does the budget decide between another attempt and
    escalation.

    `artifact_id` rather than a task id, because the rejection has to name the
    version it judged -- after a retry the task has two plans (D5).
    """
    if not issues:
        return PromotionDecision(promoted=True, next_state=STATE_PLAN_PROMOTED)

    reason = budget_exceeded(stage_attempts=stage_attempts, ticket_attempts=ticket_attempts)
    route = ROUTE_HUMAN if reason else route_for(_findings_of(issues), previous_findings)
    rejection = from_plan_issues(
        issues,
        target_artifact=artifact_id,
        attempt=stage_attempts,
        route_to=route,
        required_action=ACTION_HUMAN_REVIEW if route == ROUTE_HUMAN else ACTION_REVISE_PLAN,
    )
    return PromotionDecision(
        promoted=False,
        next_state=STATE_ESCALATED if reason else STATE_REJECTED,
        rejection=rejection,
        escalation_reason=reason,
    )


def _findings_of(issues: Sequence[PlanIssue]) -> tuple[Finding, ...]:
    """Findings for routing purposes only; the rejection builds its own."""
    from .rejection import findings_from_plan_issues

    return findings_from_plan_issues(issues)


# --------------------------------------------------------------------------- #
# What a driver does next (D9, AC12)
# --------------------------------------------------------------------------- #

BLOCKED_ON_HUMAN_APPROVAL = "human-approval"
BLOCKED_ON_ESCALATION = "escalation"
"""Why a driver stopped.

Distinct on purpose, and this is AC5's note stated in code: waiting for an
approval is a standing door every ticket passes through, while an escalation is
an exception. A runner treats both as "stop and tell someone", but they are not
the same event and must not share a reason code -- collapsing them would make
the normal case indistinguishable from a failure in the data.
"""

_STOPPED_ON = {
    STATE_PLAN_PROMOTED: BLOCKED_ON_HUMAN_APPROVAL,
    STATE_ESCALATED: BLOCKED_ON_ESCALATION,
}

_STAGE_FOR = {
    STATE_READY_FOR_PLAN: STAGE_ARCHITECT,
    STATE_REJECTED: STAGE_ARCHITECT,
}


@dataclass(frozen=True)
class NextStep:
    """The answer to "what happens next", for a person or for a runner.

    This is the interface a runner is built against (D9), which is why it is a
    value with a `to_dict` rather than printed text: the human-readable format
    will change, and anything parsing it would break.
    """

    state: str | None
    stage: str | None
    workspace: str | None
    consumes: tuple[str, ...]
    blocked_on: str | None
    attempt: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "stage": self.stage,
            "workspace": self.workspace,
            "consumes": list(self.consumes),
            "blocked_on": self.blocked_on,
            "attempt": self.attempt,
        }


STATE_LABELS = {
    STATE_READY_FOR_PLAN: "awaiting plan",
    STATE_PLAN_PROMOTED: "awaiting approval",
    STATE_PLAN_APPROVED: "plan recorded",
    STATE_REJECTED: "rejected, awaiting a new plan",
    STATE_ESCALATED: "escalated",
    STATE_ABANDONED: "abandoned",
}


def state_label(state: str) -> str:
    """A short human-readable name for a state.

    Lives here rather than in cli.py on purpose (D13). The CLI is allowed to name
    the two states it has always needed for display, and if it had to learn the
    three this slice adds in order to label them, the guard that keeps decisions
    out of the CLI could no longer tell a label apart from a judgement. Handing
    it a lookup keeps the guard meaningful.
    """
    return STATE_LABELS.get(state, state.lower().replace("_", " "))


def next_step(task: Any | None) -> NextStep:
    """What to do next for one task, or for no task at all.

    `PLAN_PROMOTED` yields no stage. That is D10 expressed as data: a runner
    asking this question is told to stop, because approval is not a transition
    it has. The gate holds by construction rather than by policy.
    """
    if task is None:
        return NextStep(
            state=None, stage=None, workspace=None, consumes=(), blocked_on=None, attempt=0
        )
    return NextStep(
        state=task.state,
        stage=_STAGE_FOR.get(task.state),
        workspace=task.worktree_path,
        # The authority table: the Architect consumes the Ticket, which is not a
        # promoted artifact. The first stage has no artifact inputs by design.
        consumes=(),
        blocked_on=_STOPPED_ON.get(task.state),
        attempt=getattr(task, "attempt", 0) or 0,
    )
