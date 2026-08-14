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

from .errors import ValidationError, WorkspaceError
from .schema import PlanIssue, validate_plan
from .workspace import PLAN_FILENAME, STATE_DIRNAME

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

TYPE_TECHNICAL_PLAN = "TechnicalPlan"
"""The Architect's artifact. The only contract registered this slice."""

TYPE_REJECTION = "Rejection"
"""A rejection is stored like any other artifact so that a retry loop leaves a
record, but it is not a contract: nobody extracts one from a workspace."""

ROLE_ARCHITECT = "architect"
ROLE_ORCHESTRATOR = "orchestrator"
"""Producer names. The other three agent roles arrive with their stages.

The Orchestrator is listed because it produces one artifact type of its own, a
rejection. That is not the same as being an agent (A-4): it produces a record of
its own deterministic judgement, and it is the only party that may promote
anything at all."""

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
    """

    id: str
    type: str
    produced_by: str
    task_id: int
    base_commit: str | None
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
"""


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
