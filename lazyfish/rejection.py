"""Rejections: why something did not get promoted, as data.

D5 requires a rejection to be a machine-readable artifact rather than a paragraph
of prose. The reason is not tidiness. A rejection has three consumers and prose
serves none of them:

  routing      "which layer does this go back to" has to be decided, not read
  escalation   "the same rule failed twice in a row" has to be comparable
  counting     `status` reports rejections, so they have to be countable

So every finding names a rule id taken from the validator (`schema.py`), and the
free text lives in `evidence`, where it is a value rather than the message.

One rule id is not a validator rule: a human rejecting a plan on judgement is
not a contract violation, and merging the two would corrupt exactly the counts
this module exists to keep separable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .schema import PlanIssue

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

SOURCE_ORCHESTRATOR = "ORCHESTRATOR"
SOURCE_HUMAN = "HUMAN"
"""Who rejected: a rule or a person. The Orchestrator judges the contract and can
do so automatically; a person judges the content, and that never becomes
automatic (D10). Recording which one happened keeps the two kinds of rejection
countable apart in `status`."""

ROUTE_ARCHITECT = "ARCHITECT"
ROUTE_HUMAN = "HUMAN"
"""Where the work goes back to. Only two destinations exist this slice, because
only one stage does, but the field is here now: adding a destination later is
cheaper than changing what the field means (D5)."""

SEVERITY_BLOCKING = "blocking"
"""Every rejection is blocking this slice -- a plan either satisfies the contract
or does not advance. The field exists because a ReviewReport's findings will not
all be blocking, and a rejection then needs to say which kind it carries."""

RULE_HUMAN_JUDGEMENT = "human-judgement"
"""The rule id for "a person read this and said no".

Deliberately outside the validator's rule set. A judgement is not a rule
violation: if it shared an id with one, "the same rule failed twice, escalate"
would fire on a person disagreeing twice, and the as-is acceptance measurement
would mix human decisions in with schema failures.
"""

ACTION_REVISE_PLAN = "Revise the plan and run promote again."
ACTION_HUMAN_REVIEW = "Two attempts failed the same rule; a person needs to look at this."


# --------------------------------------------------------------------------- #
# The artifact
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Finding:
    """One reason, as a rule id plus the evidence for it."""

    rule: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "evidence": self.evidence}


@dataclass(frozen=True)
class RejectionArtifact:
    """A rejection, addressed at the artifact it judged.

    Frozen on purpose. This is a record of a decision that was made, not a
    scratch object to update on the next attempt -- a retry produces a new
    rejection pointing at a new artifact, and the old one stays true about the
    version it actually saw.

    `target_artifact` is the artifact id, never the task id, for the same
    reason: after two attempts a task has two plans, and a rejection that named
    only the task could not say which of them it rejected.
    """

    target_artifact: str
    source: str
    severity: str
    findings: tuple[Finding, ...]
    attempt: int
    required_action: str
    route_to: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe shape. This is what gets stored and what `--json` prints."""
        return {
            "target_artifact": self.target_artifact,
            "source": self.source,
            "severity": self.severity,
            "findings": [finding.to_dict() for finding in self.findings],
            "attempt": self.attempt,
            "required_action": self.required_action,
            "route_to": self.route_to,
        }

    def rules(self) -> tuple[str, ...]:
        """The rule ids, in order. What the routing rule compares between attempts."""
        return tuple(finding.rule for finding in self.findings)


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def findings_from_plan_issues(issues: Iterable[PlanIssue]) -> tuple[Finding, ...]:
    """Map the validator's issues onto findings, one for one.

    The mapping lives here rather than in `schema.py`: the validator's job is to
    describe what is wrong to whoever fixes the plan, and it already does that
    well. Turning those descriptions into routable data is this module's job.
    """
    return tuple(
        Finding(rule=issue.rule, evidence=f"{issue.location}: {issue.message}") for issue in issues
    )


def from_plan_issues(
    issues: Sequence[PlanIssue],
    *,
    target_artifact: str,
    attempt: int,
    route_to: str = ROUTE_ARCHITECT,
    required_action: str = ACTION_REVISE_PLAN,
) -> RejectionArtifact:
    """A rejection produced by the contract check.

    `route_to` defaults to the layer that produced the artifact, which is the
    answer whenever a contract was violated. The Orchestrator overrides it when
    its routing rule says a person is needed; the decision belongs there, not
    here, so that this module stays a value constructor.
    """
    return RejectionArtifact(
        target_artifact=target_artifact,
        source=SOURCE_ORCHESTRATOR,
        severity=SEVERITY_BLOCKING,
        findings=findings_from_plan_issues(issues),
        attempt=attempt,
        required_action=required_action,
        route_to=route_to,
    )


def from_human(
    note: str,
    *,
    target_artifact: str,
    attempt: int,
    required_action: str = ACTION_REVISE_PLAN,
) -> RejectionArtifact:
    """A rejection produced by a person reading the plan (AC15's third exit).

    The note is stored exactly as it was typed, in whatever language it was
    typed in. It is the user's own text about their own ticket, and the
    passthrough rule covers it for the same reason it covers ticket content.
    """
    return RejectionArtifact(
        target_artifact=target_artifact,
        source=SOURCE_HUMAN,
        severity=SEVERITY_BLOCKING,
        findings=(Finding(rule=RULE_HUMAN_JUDGEMENT, evidence=note),),
        attempt=attempt,
        required_action=required_action,
        route_to=ROUTE_ARCHITECT,
    )
