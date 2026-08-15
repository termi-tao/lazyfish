"""Artifacts: what a stage produces, and what may cross into the next one.

The hard boundary in this design is promotion, not writing (D1). An agent works
in a disposable workspace and may change anything it likes there; the only thing
that survives the stage is an artifact extracted by contract, validated, and
promoted by the Orchestrator. Nothing here decides whether to promote -- that is
the Orchestrator's call. This module answers three narrower questions:

  what an artifact is        the Artifact record and its lineage fields
  how one is obtained        CONTRACTS: extract from a workspace, then validate
  where the content lives    ArtifactStore, outside the target repository

The identifier is content addressed rather than an autoincrement (D8), which is
what makes promoting the same plan twice idempotent and lets lineage refer to an
exact version. Because lineage is a graph of ids, an unstable id would silently
detach a rejection from the artifact it judged (R6) -- hence `canonical_bytes`
and the tests that pin it across processes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import authority
from .errors import LazyfishError, ValidationError, WorkspaceError
from .schema import PlanIssue, validate_plan
from .workspace import PLAN_FILENAME, STATE_DIRNAME

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

TYPE_TECHNICAL_PLAN = authority.TYPE_TECHNICAL_PLAN
ROLE_ARCHITECT = authority.ROLE_ARCHITECT
"""Re-exported from the authority table, which is where they are defined.

Spelled here as aliases rather than as a second pair of string literals: the
authority table decides whether a call site may produce a `TechnicalPlan` by
comparing this exact string, so two independent definitions would fail closed
and be tedious to find the day one of them was edited. That is the drift
`authority.py`'s own docstring warns about, and having written the warning it
would be poor form to be the first to ignore it. The names stay importable from
here because that is where callers have always found them."""

TYPE_REJECTION = "Rejection"
"""A rejection is stored like any other artifact so that a retry loop leaves a
record, but it is not a contract: nobody extracts one from a workspace. Defined
here and not in the authority table on purpose -- no call site produces one, so
the table has nothing to say about it."""

ROLE_ORCHESTRATOR = "orchestrator"
"""The Orchestrator produces one artifact type of its own, a rejection. That is
not the same as being an agent (A-4): it produces a record of its own
deterministic judgement, and it is the only party that may promote anything at
all. The four agent roles live in the authority table."""

PROMOTER_ORCHESTRATOR = "orchestrator"
PROMOTER_HUMAN = "human"
"""Who promoted an artifact. Normally the Orchestrator, deterministically. A
person forcing a promotion past the contract is recorded as `human` instead, so
that the override is legible in the data afterwards rather than looking like a
clean pass (AC14's approach applied to promotion)."""


# --------------------------------------------------------------------------- #
# Content addressing
# --------------------------------------------------------------------------- #


def canonical_bytes(content: Mapping[str, Any]) -> bytes:
    """Serialise content so that equal values always give equal bytes.

    Three properties matter, and each is load-bearing:

    - **Keys are sorted.** A mapping is unordered as a value, so two plans that
      differ only in key order are the same plan and must hash alike.
    - **Lists are left alone.** Sorting them would be data loss: the order of
      `changes` is part of what the Architect said.
    - **No interpreter state is involved.** `sort_keys` is a total order over
      strings, so the result does not move with PYTHONHASHSEED between runs (R6).

    `ensure_ascii=False` keeps the passthrough rule intact all the way into the
    store: a ticket written in Chinese is stored as those characters, not as
    escapes (LF-1's content rule).
    """
    text = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8")


def content_id(content: Mapping[str, Any]) -> str:
    """The artifact's identifier: a digest of its canonical bytes (D8)."""
    return hashlib.sha256(canonical_bytes(content)).hexdigest()


# --------------------------------------------------------------------------- #
# The artifact record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Artifact:
    """One artifact's identity and lineage.

    The fields are the architecture note's section 5, and none of them is
    optional decoration. `base_commit` is the point the workspace was
    materialised from, and it is read from the database rather than from git,
    because HEAD and refs are writable by the party being checked (D4).
    `parents` answers "which version of the input was this made against", which
    is the only way to tell, after a retry, which artifact a report judged.

    `call_site` is the point in the pipeline that produced it, which `produced_by`
    cannot express: a Tester writing tests and a Tester verifying an
    implementation are the same role at two call sites, and they are authorised
    to produce different things. NULL on rows written before LF-6; authority
    falls back to the weaker role-level question for those (`ensure_authorized`).
    """

    id: str
    type: str
    produced_by: str
    task_id: int
    base_commit: str | None
    call_site: str | None = None
    parents: tuple[str, ...] = ()
    promoted_at: str | None = None
    attempt: int = 0
    promoted_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Shape used by `--json` output and by the database row."""
        return {
            "id": self.id,
            "type": self.type,
            "produced_by": self.produced_by,
            "call_site": self.call_site,
            "task_id": self.task_id,
            "base_commit": self.base_commit,
            "parents": list(self.parents),
            "promoted_at": self.promoted_at,
            "attempt": self.attempt,
            "promoted_by": self.promoted_by,
        }


# --------------------------------------------------------------------------- #
# The contract registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Contract:
    """How one artifact type is obtained from a workspace and judged.

    `extract` is the reason this model is language agnostic: it reads what the
    contract names, not whatever happens to have changed. An Architect that also
    implemented the whole ticket produces exactly the same artifact as one that
    only wrote the plan, because the other twenty files are never looked at
    (AC1). There is no path knowledge here to become wrong on a repository
    laid out differently.
    """

    type: str
    produced_by: str
    extract: Callable[[Path], dict[str, Any]]
    validate: Callable[..., list[PlanIssue]]


def _extract_technical_plan(workspace: Path) -> dict[str, Any]:
    """Read plan.json out of a workspace, and nothing else."""
    path = Path(workspace) / STATE_DIRNAME / PLAN_FILENAME
    if not path.exists():
        raise ValidationError(
            f"No {PLAN_FILENAME} at {path}\n"
            f"The design phase writes it there. Run your AI tool inside the "
            f"workspace first, then try again."
        )
    try:
        with open(path, encoding="utf-8") as handle:
            content = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"{path} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(content, dict):
        raise ValidationError(f"{path} must contain a JSON object, not {type(content).__name__}.")
    return content


def _validate_technical_plan(
    content: Mapping[str, Any], workspace: Path | None = None
) -> list[PlanIssue]:
    """Judge a plan with the rules that already exist.

    A2: the TechnicalPlan contract is not new work. `plan-schema.json` plus the
    three extra rules in `schema.py` are the contract, and reusing them is what
    keeps a rejection's findings expressed as rule ids rather than prose.
    """
    return validate_plan(dict(content), workspace)


CONTRACTS: dict[str, Contract] = {
    TYPE_TECHNICAL_PLAN: Contract(
        type=TYPE_TECHNICAL_PLAN,
        produced_by=ROLE_ARCHITECT,
        extract=_extract_technical_plan,
        validate=_validate_technical_plan,
    ),
}
"""Registered contracts, one per artifact type an agent can produce.

Deliberately one entry. Bringing four unverified contracts up at once was
rejected (A-3): when something misbehaves there would be no way to tell which
one. This is a registry, not a configurable abstraction layer -- adding the next
stage means adding an entry, not designing a schema for contracts.

LF-6 defines the other three contracts and this slice does not register them.
Nothing extracts or validates an artifact at a call site that cannot yet run, so
a registered entry would be a surface with no entry point -- the same thing LF-6
refused for states, for the same reason. What materialisation genuinely needs
from those contracts is one bit, whether the type carries a patch, and that is
`PATCH_CARRYING_TYPES` below. The registration belongs with the stage that first
calls it. (Reported: LF-6's `changes` asks for registration now, while its AC7
forbids turning an existing test red, and
`test_only_the_technical_plan_contract_is_registered` pins this set. AC7 wins
until the plan says otherwise.)
"""


# --------------------------------------------------------------------------- #
# Authority
# --------------------------------------------------------------------------- #

PATCH_CARRYING_TYPES: frozenset[str] = frozenset(
    {authority.TYPE_TEST_ARTIFACT, authority.TYPE_IMPLEMENTATION_PATCH}
)
"""Artifact types whose content includes a `patch` to be applied on top of the
base commit. Everything else is data: it travels as context and changes no file.

One rule covering every contract rather than a flag per contract, because
materialisation only needs to ask one question of an artifact. The patch is a
unified diff and never a commit: a commit would mean trusting refs inside a
workspace the party being checked can write.
"""

PATCH_FIELD = "patch"


def carries_patch(artifact_type: str) -> bool:
    """Whether materialisation applies this type's content to the workspace."""
    return artifact_type in PATCH_CARRYING_TYPES


def ensure_authorized(artifact: Artifact) -> None:
    """Refuse a promotion the authority table does not permit (AC2, AC3).

    Called by `promote`, which is the only place authority can be enforced
    without trusting anyone: an agent may write whatever it likes in its own
    workspace, and the question is only ever which of that becomes the version
    downstream is built from.

    A type outside the table is deliberately left alone, because a Rejection is
    the Orchestrator's own record of its own judgement and no call site produces
    one.

    A row with no `call_site` is the pre-LF-6 shape, and it is answered only when
    the role leaves no room for a question: exactly one call site. That is not a
    narrowing of the fallback's purpose, it is its purpose stated precisely --
    every artifact a pre-LF-6 database can hold is an architect's TechnicalPlan,
    and the architect has one call site, so nothing that fallback exists for is
    turned away. Asking the looser question instead ("may this *role* produce
    this type anywhere?") would make a missing call site into a way around the
    table for exactly the roles the table was reshaped to split: with two call
    sites, a NULL would let either one's output through as the other's.
    """
    if artifact.type not in authority.GOVERNED_TYPES:
        return
    if artifact.call_site is None:
        sites = authority.call_sites_for(artifact.produced_by)
        if len(sites) == 1 and authority.may_produce(sites[0], artifact.type):
            return
        if len(sites) == 1:
            raise LazyfishError(
                f"{sites[0]} is not authorized to produce a {artifact.type}, and "
                f"that is the only call site the {artifact.produced_by} role has."
            )
        raise LazyfishError(
            f"This {artifact.type} records no call site, so it cannot be judged: "
            f"the {artifact.produced_by} role runs at {len(sites)} call sites and "
            f"they are not authorized to produce the same things. Record the call "
            f"site that produced it."
        )
    if authority.may_produce(artifact.call_site, artifact.type):
        return
    site = authority.CALL_SITES.get(artifact.call_site)
    permitted = ", ".join(site.produces) if site else "nothing (no such call site)"
    raise LazyfishError(
        f"{artifact.call_site} is not authorized to produce a {artifact.type}. "
        f"That call site may produce: {permitted}."
    )


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ArtifactStore:
    """Artifact content on disk, addressed by task and id.

    Content lives under the lazyfish data directory, never in the target
    repository (D8, A-5). Two reasons: the workspace is discarded, and writing
    tool state into a repository the user owns is the same instinct LF-1 ruled
    against when it refused to search for configuration there.
    """

    root: Path = field()

    def path_for(self, task_id: int, artifact_id: str) -> Path:
        """Where one artifact's content lives. Part of the design, not detail."""
        return Path(self.root) / str(task_id) / artifact_id

    def store(self, artifact: Artifact, content: Mapping[str, Any]) -> Path:
        """Write content and return its path. Idempotent for equal content.

        Because the filename is the digest of the bytes, storing the same plan
        again resolves to the same file; the bytes are only rewritten when they
        would differ, which they cannot. A retry with a different plan lands
        beside the first rather than over it -- several artifacts per stage is
        the normal shape of a rejection loop, and losing the earlier one would
        break the lineage a rejection points at.
        """
        path = self.path_for(artifact.task_id, artifact.id)
        payload = canonical_bytes(content)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_bytes() != payload:
                path.write_bytes(payload)
        except OSError as exc:
            raise WorkspaceError(
                f"Could not write artifact {artifact.id} to {path}: {exc}"
            ) from exc
        return path

    def load(self, task_id: int, artifact_id: str) -> dict[str, Any]:
        """Read stored content back."""
        path = self.path_for(task_id, artifact_id)
        if not path.exists():
            raise WorkspaceError(
                f"No stored artifact {artifact_id} for task {task_id} (looked in {path})."
            )
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"Stored artifact {path} could not be read: {exc}") from exc
