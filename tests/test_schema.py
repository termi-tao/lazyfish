"""plan.json validation, with the needs_human rule getting the most attention.

If that rule is wrong the whole review mechanism is decorative: a plan nobody
looked at would be recorded as if it had been reviewed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lazyfish.errors import ValidationError
from lazyfish.schema import (
    RULE_FILES_EXIST,
    RULE_NEEDS_HUMAN,
    RULE_REQUIRED_ARRAYS,
    RULE_SCHEMA,
    load_plan,
    validate_plan,
)

from .conftest import make_plan


def rules(issues: list) -> set[str]:
    return {issue.rule for issue in issues}


def test_a_complete_plan_validates(repo: Path) -> None:
    assert validate_plan(make_plan(), repo) == []


def test_missing_required_key_is_a_schema_error(repo: Path) -> None:
    plan = make_plan()
    del plan["acceptance_criteria"]
    issues = validate_plan(plan, repo)
    assert RULE_SCHEMA in rules(issues)
    assert any("acceptance_criteria" in issue.message for issue in issues)


def test_unknown_key_is_rejected(repo: Path) -> None:
    issues = validate_plan(make_plan(estimate="2 days"), repo)
    assert RULE_SCHEMA in rules(issues)


def test_bad_enum_value_is_reported_with_its_location(repo: Path) -> None:
    plan = make_plan()
    plan["changes"][0]["action"] = "refactor"
    issues = validate_plan(plan, repo)
    assert any(issue.location.startswith("plan.changes.0") for issue in issues)


# --------------------------------------------------------------------------- #
# Rule 1: the three arrays
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["assumptions", "alternatives_considered", "open_questions"])
def test_empty_required_array_fails(repo: Path, key: str) -> None:
    issues = validate_plan(make_plan(**{key: []}), repo)
    assert RULE_REQUIRED_ARRAYS in rules(issues)
    assert any(key in issue.location for issue in issues if issue.rule == RULE_REQUIRED_ARRAYS)


def test_all_three_arrays_reported_at_once(repo: Path) -> None:
    """Every problem in one run: fixing plans one error at a time is friction."""
    issues = validate_plan(
        make_plan(assumptions=[], alternatives_considered=[], open_questions=[]), repo
    )
    array_issues = [issue for issue in issues if issue.rule == RULE_REQUIRED_ARRAYS]
    assert len(array_issues) == 3


# --------------------------------------------------------------------------- #
# Rule 2: needs_human
# --------------------------------------------------------------------------- #


def test_open_questions_force_needs_human(repo: Path) -> None:
    """AC8: the case that matters most."""
    issues = validate_plan(make_plan(needs_human=False), repo)
    needs_human = [issue for issue in issues if issue.rule == RULE_NEEDS_HUMAN]
    assert len(needs_human) == 1
    assert "open_questions is not empty" in needs_human[0].message


def test_low_confidence_forces_needs_human(repo: Path) -> None:
    plan = make_plan(
        confidence="low",
        needs_human=False,
        open_questions=[{"text": "settled: use the existing helper", "blocking": False}],
    )
    issues = validate_plan(plan, repo)
    message = next(issue.message for issue in issues if issue.rule == RULE_NEEDS_HUMAN)
    assert "confidence is 'low'" in message


def test_needs_human_true_is_always_acceptable(repo: Path) -> None:
    plan = make_plan(confidence="low", needs_human=True)
    assert [issue for issue in validate_plan(plan, repo) if issue.rule == RULE_NEEDS_HUMAN] == []


def test_needs_human_rule_does_not_fire_without_a_trigger(repo: Path) -> None:
    """With no open questions and high confidence, needs_human may be false.

    The rule is a floor, not a demand that every plan be escalated. Rule 1 keeps
    this combination out of real plans; the check is still written so that the
    two rules stay independent.
    """
    plan = make_plan(needs_human=False, open_questions=[])
    assert [issue for issue in validate_plan(plan, repo) if issue.rule == RULE_NEEDS_HUMAN] == []


# --------------------------------------------------------------------------- #
# Rule 3: referenced files exist
# --------------------------------------------------------------------------- #


def test_missing_file_is_reported_with_the_path(repo: Path) -> None:
    """AC9."""
    plan = make_plan(
        changes=[
            {
                "file": "src/auth/does_not_exist.py",
                "action": "modify",
                "reason": "typo in the plan",
            }
        ]
    )
    issues = validate_plan(plan, repo)
    assert RULE_FILES_EXIST in rules(issues)
    assert "src/auth/does_not_exist.py" in issues[0].message


def test_created_files_are_exempt(repo: Path) -> None:
    plan = make_plan(
        changes=[
            {
                "file": "src/auth/new_module.py",
                "action": "create",
                "reason": "new behaviour needs a home",
            }
        ]
    )
    assert validate_plan(plan, repo) == []


def test_file_existence_is_skipped_without_a_worktree() -> None:
    plan = make_plan(
        changes=[{"file": "anything.py", "action": "modify", "reason": "no worktree given"}]
    )
    assert validate_plan(plan, None) == []


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_missing_plan_file_says_where_it_should_be(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as excinfo:
        load_plan(tmp_path / ".lazyfish" / "plan.json")
    assert "plan.json" in str(excinfo.value)


def test_malformed_json_reports_the_position(tmp_path: Path) -> None:
    broken = tmp_path / "plan.json"
    broken.write_text('{"ticket": "PROJ-1",}', encoding="utf-8")
    with pytest.raises(ValidationError, match="line 1"):
        load_plan(broken)


def test_top_level_array_is_rejected(tmp_path: Path) -> None:
    wrong = tmp_path / "plan.json"
    wrong.write_text("[]", encoding="utf-8")
    with pytest.raises(ValidationError, match="JSON object"):
        load_plan(wrong)


def test_round_trip_through_disk(tmp_path: Path, repo: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(make_plan(), ensure_ascii=False), encoding="utf-8")
    assert validate_plan(load_plan(path), repo) == []
