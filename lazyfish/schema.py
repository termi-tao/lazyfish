"""plan.json validation.

Two layers:

1. JSON Schema, for shape.
2. Three rules the schema cannot express, checked afterwards.

Both layers return a list of `PlanIssue` rather than raising, so the CLI can
print every problem at once. Fixing plans one error per run is the friction that
makes people abandon a tool (R3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from .errors import ValidationError

RULE_SCHEMA = "schema"
RULE_REQUIRED_ARRAYS = "non-empty-arrays"
RULE_NEEDS_HUMAN = "needs-human"
RULE_FILES_EXIST = "changes-file-exists"

REQUIRED_NON_EMPTY = ("assumptions", "alternatives_considered", "open_questions")


@dataclass(frozen=True)
class PlanIssue:
    """One validation failure, addressed to the person fixing the plan."""

    rule: str
    location: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.location}: {self.message}"


def schema_text() -> str:
    """The JSON Schema as text, for embedding in the design prompt."""
    return (
        resources.files("lazyfish.templates")
        .joinpath("plan-schema.json")
        .read_text(encoding="utf-8")
    )


def load_schema() -> dict[str, Any]:
    return json.loads(schema_text())


def load_plan(path: Path) -> dict[str, Any]:
    """Read plan.json, with errors aimed at whoever has to fix the file."""
    if not path.exists():
        raise ValidationError(
            f"No plan file at {path}\n"
            f"The design phase writes it there. Run your AI tool inside the "
            f"worktree first, then try again."
        )
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"{path} is not valid JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise ValidationError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(
            f"{path} must contain a JSON object at the top level, got {type(data).__name__}."
        )
    return data


def _location(error: jsonschema.ValidationError) -> str:
    parts = [str(part) for part in error.absolute_path]
    return "plan" + ("." + ".".join(parts) if parts else "")


def validate_schema(plan: dict[str, Any]) -> list[PlanIssue]:
    validator = jsonschema.Draft202012Validator(load_schema())
    return [
        PlanIssue(RULE_SCHEMA, _location(error), error.message)
        for error in sorted(validator.iter_errors(plan), key=lambda e: list(e.absolute_path))
    ]


def _has_open_questions(plan: dict[str, Any]) -> bool:
    value = plan.get("open_questions")
    return isinstance(value, list) and len(value) > 0


def validate_extra_rules(plan: dict[str, Any], worktree: Path | None = None) -> list[PlanIssue]:
    """The three checks JSON Schema cannot express.

    Note how rules 1 and 2 interact: open_questions must not be empty, and a
    non-empty open_questions forces needs_human. That is intentional for this
    slice. A plan produced in one pass has not been reviewed by anyone, and the
    honest value of needs_human at that moment is true; the field records
    whether review happened, not how confident the author felt.
    """
    issues: list[PlanIssue] = []

    for key in REQUIRED_NON_EMPTY:
        value = plan.get(key)
        if not isinstance(value, list) or len(value) == 0:
            issues.append(
                PlanIssue(
                    RULE_REQUIRED_ARRAYS,
                    f"plan.{key}",
                    f"must be a non-empty array; an empty {key} means the question "
                    f"was skipped, not that there is nothing to say",
                )
            )

    confidence = plan.get("confidence")
    needs_human = plan.get("needs_human")
    if needs_human is not True:
        reasons = []
        if confidence == "low":
            reasons.append("confidence is 'low'")
        if _has_open_questions(plan):
            reasons.append("open_questions is not empty")
        if reasons:
            issues.append(
                PlanIssue(
                    RULE_NEEDS_HUMAN,
                    "plan.needs_human",
                    f"must be true because {' and '.join(reasons)}; got {needs_human!r}",
                )
            )

    if worktree is not None:
        changes = plan.get("changes")
        if isinstance(changes, list):
            for index, change in enumerate(changes):
                if not isinstance(change, dict):
                    continue
                action = change.get("action")
                target = change.get("file")
                if action == "create" or not isinstance(target, str) or not target:
                    continue
                if not (worktree / target).exists():
                    issues.append(
                        PlanIssue(
                            RULE_FILES_EXIST,
                            f"plan.changes[{index}].file",
                            f"path does not exist in the worktree: {target} "
                            f"(action is '{action}', not 'create'). Checked under "
                            f"{worktree}",
                        )
                    )
    return issues


def validate_plan(plan: dict[str, Any], worktree: Path | None = None) -> list[PlanIssue]:
    """Full validation. Empty list means the plan is acceptable."""
    issues = validate_schema(plan)
    issues.extend(validate_extra_rules(plan, worktree))
    return issues
