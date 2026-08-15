"""The authority table: who may produce what, and what each call site reads.

This is the boundary itself, expressed as data. Two questions are answered here
and nowhere else:

  produces   which artifact types a call site is permitted to promote (AC3)
  consumes   which inputs its workspace is materialised from (AC5)

Both are properties of a *call site*, not of a role. Tester and Reviewer each
have two, and their two differ in exactly the way that makes the boundary worth
having: `tester@write` cannot have the implementation in its hands, because at
that point it does not exist, while `tester@verify` must. Collapsing either pair
into one role-shaped row would leave "the Tester cannot see the implementation
while writing the tests" with nowhere to live, which is the whole value of
writing tests before the implementation.

Nothing here does I/O, and nothing here decides anything. `promote` asks whether
a combination is permitted (`artifacts.ensure_authorized`) and `materialize`
asks what to build a workspace from (`workspace.materialize`); both read this
table rather than keeping a second copy of it. One fact, one source: two
descriptions of the same rule drift, which is the lesson LF-2 paid for.

This is a hard-coded registry, not a configurable schema. An agent contract
format stays deferred; adding a call site means adding a row.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LazyfishError

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

ROLE_ARCHITECT = "architect"
ROLE_TESTER = "tester"
ROLE_CODER = "coder"
ROLE_REVIEWER = "reviewer"
"""The four agent roles. The Orchestrator is not among them: it is the
deterministic control layer, and the only party that may promote anything."""

CALL_SITE_ARCHITECT = "architect"
CALL_SITE_TESTER_WRITE = "tester@write"
CALL_SITE_TESTER_VERIFY = "tester@verify"
CALL_SITE_CODER = "coder"
CALL_SITE_REVIEWER_TESTS = "reviewer@tests"
CALL_SITE_REVIEWER_IMPL = "reviewer@impl"
"""Call sites, spelled `role@point` where a role has more than one.

There are more call sites than roles, and any count taken from the roles is
wrong. The table below is the only place the list exists.
"""

TYPE_TECHNICAL_PLAN = "TechnicalPlan"
TYPE_TEST_ARTIFACT = "TestArtifact"
TYPE_TEST_REPORT = "TestReport"
TYPE_IMPLEMENTATION_PATCH = "ImplementationPatch"
TYPE_REVIEW_REPORT = "ReviewReport"
"""The artifact types the table governs.

`Rejection` is deliberately absent. It is produced by the Orchestrator as the
record of its own deterministic judgement, not by an agent, so no call site
produces it and the table has nothing to say about it.
"""

INPUT_TICKET = "Ticket"
INPUT_ACCEPTANCE_CRITERIA = "acceptance_criteria"
"""Inputs that are read but never promoted.

They appear in `consumes` beside artifact types because a call site's read
surface is one list, not two. Materialisation ignores them: they reach a
workspace as context files, not as a patch applied to the base commit.
"""

PSEUDO_INPUTS = frozenset({INPUT_TICKET, INPUT_ACCEPTANCE_CRITERIA})
"""The two above, as a set, for callers that need the artifact half of `consumes`.

`next_step` is one: what a runner has to fetch and apply is the promoted
artifacts, and a context file the workspace already contains is not something it
can act on.
"""


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CallSite:
    """One row: a point in the pipeline, what it may produce, what it reads."""

    name: str
    role: str
    produces: tuple[str, ...]
    consumes: tuple[str, ...]
    human_gate: bool = False
    """Whether a person approves this call site's artifact before the next runs.

    The third question this table answers, and it is here for the same reason as
    the other two: it is a fact about a call site, and the alternative is for
    `accept` to work it out from the state, which cannot -- states are
    stage-independent by design (LF-7 D1), so `PROMOTED -> APPROVED` is a legal
    edge everywhere and approving at a stage with no gate would be accepted in
    silence. That row would then be indistinguishable from a real approval in
    the statistics `plan_accepted` feeds (LF-7 D4).

    Exactly one call site has a gate today, and the design says approval of the
    plan is the one decision that is never automated (D10).
    """


CALL_SITES: dict[str, CallSite] = {
    CALL_SITE_ARCHITECT: CallSite(
        name=CALL_SITE_ARCHITECT,
        role=ROLE_ARCHITECT,
        produces=(TYPE_TECHNICAL_PLAN,),
        consumes=(INPUT_TICKET,),
        human_gate=True,
    ),
    # No implementation and no tests: the tests are what it is here to write,
    # and it has to write them against the ticket rather than against code that
    # already passes.
    CALL_SITE_TESTER_WRITE: CallSite(
        name=CALL_SITE_TESTER_WRITE,
        role=ROLE_TESTER,
        produces=(TYPE_TEST_ARTIFACT,),
        consumes=(INPUT_TICKET, INPUT_ACCEPTANCE_CRITERIA),
    ),
    # The same role, the opposite read surface. Verification happens against the
    # promoted tests and the promoted implementation, so whatever the Coder did
    # to the tests in its own workspace was never in this one.
    CALL_SITE_TESTER_VERIFY: CallSite(
        name=CALL_SITE_TESTER_VERIFY,
        role=ROLE_TESTER,
        produces=(TYPE_TEST_REPORT,),
        consumes=(TYPE_TEST_ARTIFACT, TYPE_IMPLEMENTATION_PATCH),
    ),
    # The Coder's patch lands on top of the promoted TestArtifact. That, and not
    # a rule about which paths it may write, is why it cannot change the tests:
    # its own edits to them live in a workspace nothing downstream is built from.
    CALL_SITE_CODER: CallSite(
        name=CALL_SITE_CODER,
        role=ROLE_CODER,
        produces=(TYPE_IMPLEMENTATION_PATCH,),
        consumes=(TYPE_TECHNICAL_PLAN, TYPE_TEST_ARTIFACT),
    ),
    # Reviewing the tests before the implementation exists. A review that comes
    # after a working implementation faces a screen of green, which is the one
    # condition under which a wrong test is hardest to see.
    CALL_SITE_REVIEWER_TESTS: CallSite(
        name=CALL_SITE_REVIEWER_TESTS,
        role=ROLE_REVIEWER,
        produces=(TYPE_REVIEW_REPORT,),
        consumes=(INPUT_ACCEPTANCE_CRITERIA, TYPE_TEST_ARTIFACT, TYPE_TEST_REPORT),
    ),
    # The largest read surface in the system, and the smallest authority: one
    # report. That gap is why the Reviewer's boundary has to be structural.
    CALL_SITE_REVIEWER_IMPL: CallSite(
        name=CALL_SITE_REVIEWER_IMPL,
        role=ROLE_REVIEWER,
        produces=(TYPE_REVIEW_REPORT,),
        consumes=(
            TYPE_TECHNICAL_PLAN,
            TYPE_TEST_ARTIFACT,
            TYPE_TEST_REPORT,
            TYPE_IMPLEMENTATION_PATCH,
            TYPE_REVIEW_REPORT,
        ),
    ),
}
"""The whole table. The list of call sites exists here and is quoted elsewhere.

Every row is present from this slice on, including the two whose stage is not
implemented yet. Authority is not the same thing as execution: writing the row
down enforces "the Reviewer may not promote an implementation" today, without
any Reviewer having to run.
"""

GOVERNED_TYPES: frozenset[str] = frozenset(
    type_name for site in CALL_SITES.values() for type_name in site.produces
)
"""Artifact types some call site is responsible for.

Derived rather than listed, so a new row cannot forget to extend it. A type
outside this set is not an agent's output and the table does not judge it.
"""


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def _require(call_site: str) -> CallSite:
    site = CALL_SITES.get(call_site)
    if site is None:
        known = ", ".join(sorted(CALL_SITES))
        raise LazyfishError(f"Unknown call site {call_site!r}. Known call sites: {known}.")
    return site


def produces_for(call_site: str) -> tuple[str, ...]:
    """Artifact types this call site is authorised to produce."""
    return _require(call_site).produces


def consumes_for(call_site: str) -> tuple[str, ...]:
    """Inputs this call site's workspace is materialised from (AC5)."""
    return _require(call_site).consumes


def consumes_artifacts_for(call_site: str) -> tuple[str, ...]:
    """The artifact types in this call site's read surface, pseudo-inputs removed."""
    return tuple(item for item in consumes_for(call_site) if item not in PSEUDO_INPUTS)


def has_human_gate(call_site: str) -> bool:
    """Whether a person approves this call site's artifact (LF-7 D4).

    An unknown call site has no gate, for the same reason an unknown one may not
    produce anything: the safe answer to "should a person be asked about this
    thing I do not recognise" is that there is nothing here to approve.
    """
    site = CALL_SITES.get(call_site)
    return site is not None and site.human_gate


def may_produce(call_site: str, artifact_type: str) -> bool:
    """Whether this call site may produce this artifact type.

    An unknown call site is simply not authorised, rather than an error: this is
    the question promotion asks, and the answer to "may this unrecognised party
    promote something" is no.
    """
    site = CALL_SITES.get(call_site)
    return site is not None and artifact_type in site.produces


def call_sites_for(role: str) -> tuple[str, ...]:
    """Every call site a role runs at, in table order.

    Asked by `ensure_authorized` when an artifact records no call site: a role
    with one call site can still be judged, a role with two cannot, and the
    difference is the whole reason the table is keyed by call site.
    """
    return tuple(name for name, site in CALL_SITES.items() if site.role == role)
