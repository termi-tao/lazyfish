"""The second stage: its workspace, its contract, and what promoting it does.

The first stage that is not the Architect, so several things are being asserted
for the first time rather than re-asserted: a workspace built by materialisation
rather than by `prep`, a contract that is a diff rather than a file, and a
promotion that advances on its own because nobody approves it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from lazyfish.artifacts import (
    RULE_AC_COVERED,
    RULE_AC_ID_KNOWN,
    RULE_PATCH_NOT_EMPTY,
    RULE_TEST_DECLARED,
)
from lazyfish.cli import cli
from lazyfish.db import STATE_AWAITING_ARTIFACT, STATE_PROMOTED

from .conftest import (
    FakeTracker,
    open_database,
    task_of,
    workspace_for_tests,
    write_plan,
    write_tests,
)

TWO_CRITERIA = [
    "A link generated now is still accepted 23 hours later.",
    "A link older than the window is refused.",
]


def at_the_tests_stage(
    runner: CliRunner,
    configured: dict[str, Path],
    tracker: FakeTracker,
    *,
    criteria: list[str] | None = None,
) -> tuple[dict[str, Path], Path]:
    """prep, plan, accept -- leaving the ticket at `tester@write`."""
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    architect = Path(result.stdout.strip().splitlines()[-1][3:])

    write_plan(architect, acceptance_criteria=criteria or TWO_CRITERIA)
    accepted = runner.invoke(cli, ["accept", "--as-is"])
    assert accepted.exit_code == 0, accepted.stdout + accepted.stderr
    return configured, architect


# --------------------------------------------------------------------------- #
# AC1 / AC2: the workspace
# --------------------------------------------------------------------------- #


def test_accept_leaves_the_ticket_at_the_tests_stage(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC1: approving the plan is no longer the end of the ticket."""
    env, _ = at_the_tests_stage(runner, configured, tracker)

    task = task_of(env)
    assert task.state == STATE_AWAITING_ARTIFACT
    assert task.current_stage == "tester@write"
    assert workspace_for_tests(env).is_dir()


def test_the_tests_workspace_is_the_baseline_not_the_design_copy(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker, repo: Path
) -> None:
    """AC2: tests written against passing code describe it instead of constraining it."""
    env, architect = at_the_tests_stage(runner, configured, tracker)

    # Whatever the design stage did to its own copy is not here.
    (architect / "src" / "auth" / "reset_token.py").write_text(
        "RESET_TOKEN_TTL = 86400  # fixed while designing\n", encoding="utf-8"
    )
    workspace = workspace_for_tests(env)

    assert (workspace / "src" / "auth" / "reset_token.py").read_text(encoding="utf-8") == (
        repo / "src" / "auth" / "reset_token.py"
    ).read_text(encoding="utf-8")
    assert not (workspace / ".lazyfish" / "plan.json").exists()


def test_the_workspace_carries_the_approved_criteria_and_a_brief(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC4: the numbering the Tester is shown is the numbering the rule checks."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    state = workspace_for_tests(env) / ".lazyfish"

    acceptance = (state / "acceptance.md").read_text(encoding="utf-8")
    for index, text in enumerate(TWO_CRITERIA):
        assert f"**AC{index + 1}**" in acceptance
        assert text in acceptance

    assert (state / "tests-prompt.md").exists()
    assert (workspace_for_tests(env) / "CLAUDE.md").exists()


def test_the_criteria_numbering_is_generated_once(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC4: two functions agreeing by convention is how they end up off by one."""
    from lazyfish.artifacts import ac_id_for

    env, _ = at_the_tests_stage(runner, configured, tracker)
    acceptance = (workspace_for_tests(env) / ".lazyfish" / "acceptance.md").read_text(
        encoding="utf-8"
    )
    # The last criterion is declared under the id the contract will accept, and
    # one past it is not present at all.
    assert f"**{ac_id_for(len(TWO_CRITERIA) - 1)}**" in acceptance
    assert f"**{ac_id_for(len(TWO_CRITERIA))}**" not in acceptance


# --------------------------------------------------------------------------- #
# AC1: the stage completes the ticket
# --------------------------------------------------------------------------- #


def test_promoting_the_tests_completes_the_ticket(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC1: no human gate here, so the artifact passing is the whole event."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    write_tests(workspace_for_tests(env), criteria=len(TWO_CRITERIA))

    result = runner.invoke(cli, ["promote"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "TestArtifact" in result.stdout

    # No human gate here, so passing the contract is the whole event: the ticket
    # moves to the next stage rather than waiting for an approval.
    task = task_of(env)
    assert task.state == STATE_AWAITING_ARTIFACT
    assert task.current_stage == "coder"
    assert task.state != STATE_PROMOTED


def test_the_promoted_artifact_holds_the_tests(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    env, _ = at_the_tests_stage(runner, configured, tracker)
    write_tests(
        workspace_for_tests(env),
        path="tests/test_reset.py",
        body="def test_generated():\n    assert MARKER\n",
        criteria=len(TWO_CRITERIA),
    )
    assert runner.invoke(cli, ["promote"]).exit_code == 0

    with open_database(env) as database:
        task = database.get_by_ticket("PROJ-1", "work")
        assert task is not None
        artifacts = [a for a in database.list_artifacts(task.id) if a.type == "TestArtifact"]
    assert len(artifacts) == 1
    assert artifacts[0].promoted_at is not None
    assert artifacts[0].call_site == "tester@write"

    from .conftest import stored_content

    content = stored_content(env, task.id, artifacts[0].id)
    assert "tests/test_reset.py" in content["files"]
    assert "MARKER" in content["patch"]


def test_the_tool_s_own_files_stay_out_of_the_patch(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC6: CLAUDE.md and .lazyfish/ were written by lazyfish, not by the stage."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    write_tests(workspace_for_tests(env), criteria=len(TWO_CRITERIA))
    assert runner.invoke(cli, ["promote"]).exit_code == 0

    with open_database(env) as database:
        task = database.get_by_ticket("PROJ-1", "work")
        assert task is not None
        artifact = [a for a in database.list_artifacts(task.id) if a.type == "TestArtifact"][0]

    from .conftest import stored_content

    content = stored_content(env, task.id, artifact.id)
    assert "CLAUDE.md" not in content["files"]
    assert not any(str(name).startswith(".lazyfish") for name in content["files"])


def test_a_new_file_reaches_the_patch(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """R4: new test files are untracked, and a plain `git diff` would miss them."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    workspace = workspace_for_tests(env)
    write_tests(workspace, path="tests/brand_new.py", criteria=len(TWO_CRITERIA))
    assert subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain", "tests/brand_new.py"],
        capture_output=True,
        text=True,
    ).stdout.startswith("??")

    assert runner.invoke(cli, ["promote"]).exit_code == 0
    with open_database(env) as database:
        task = database.get_by_ticket("PROJ-1", "work")
        assert task is not None
        artifact = [a for a in database.list_artifacts(task.id) if a.type == "TestArtifact"][0]
    from .conftest import stored_content

    assert "tests/brand_new.py" in stored_content(env, task.id, artifact.id)["files"]


# --------------------------------------------------------------------------- #
# AC3: each rule can fail, and says which one did
# --------------------------------------------------------------------------- #


def rejection_rules(env: dict[str, Path]) -> list[str]:
    """The rule ids of the most recent rejection."""
    with open_database(env) as database:
        task = database.get_by_ticket("PROJ-1", "work")
        assert task is not None
        rejections = [a for a in database.list_artifacts(task.id) if a.type == "Rejection"]
        assert rejections, "no rejection was recorded"
        from .conftest import stored_content

        content = stored_content(env, task.id, rejections[-1].id)
    return [finding["rule"] for finding in content["findings"]]


def test_an_untouched_workspace_is_refused(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC3: patch-not-empty."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    state = workspace_for_tests(env) / ".lazyfish"
    state.mkdir(parents=True, exist_ok=True)
    (state / "tests-coverage.json").write_text(
        json.dumps({"coverage": [{"ac_id": "AC1", "test_ids": ["tests/t.py::a"]}]}),
        encoding="utf-8",
    )

    assert runner.invoke(cli, ["promote"]).exit_code == 6
    assert RULE_PATCH_NOT_EMPTY in rejection_rules(env)


def test_a_criterion_that_does_not_exist_is_refused(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC3: ac-id-known."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    write_tests(
        workspace_for_tests(env),
        coverage=[
            {"ac_id": "AC1", "test_ids": ["tests/test_generated.py::test_generated"]},
            {"ac_id": "AC2", "test_ids": ["tests/test_generated.py::test_generated"]},
            {"ac_id": "AC9", "test_ids": ["tests/test_generated.py::test_generated"]},
        ],
    )

    assert runner.invoke(cli, ["promote"]).exit_code == 6
    assert RULE_AC_ID_KNOWN in rejection_rules(env)


def test_an_uncovered_criterion_is_refused(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC3: ac-covered. The rule the whole coverage table exists for."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    write_tests(workspace_for_tests(env), criteria=1)  # two criteria, one declared

    assert runner.invoke(cli, ["promote"]).exit_code == 6
    rules = rejection_rules(env)
    assert RULE_AC_COVERED in rules


def test_a_test_in_a_file_the_patch_never_touched_is_refused(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC3: test-declared, the rule that stops an invented coverage table."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    write_tests(
        workspace_for_tests(env),
        coverage=[
            {"ac_id": "AC1", "test_ids": ["tests/test_generated.py::test_generated"]},
            {"ac_id": "AC2", "test_ids": ["tests/imaginary.py::test_that_was_never_written"]},
        ],
    )

    assert runner.invoke(cli, ["promote"]).exit_code == 6
    assert RULE_TEST_DECLARED in rejection_rules(env)


def test_a_rejection_here_routes_back_to_the_tests_stage(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """LF-7 D6: routing speaks call sites, so it can name which Tester run."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    write_tests(workspace_for_tests(env), criteria=1)
    assert runner.invoke(cli, ["promote"]).exit_code == 6

    with open_database(env) as database:
        task = database.get_by_ticket("PROJ-1", "work")
        assert task is not None
        rejection = [a for a in database.list_artifacts(task.id) if a.type == "Rejection"][-1]
        from .conftest import stored_content

        content = stored_content(env, task.id, rejection.id)
    assert content["route_to"] == "tester@write"


def test_a_corrected_table_promotes_after_a_rejection(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """The retry loop works at this stage too, not only at the Architect's."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    workspace = workspace_for_tests(env)
    write_tests(workspace, criteria=1)
    assert runner.invoke(cli, ["promote"]).exit_code == 6

    write_tests(workspace, criteria=len(TWO_CRITERIA))
    assert runner.invoke(cli, ["promote"]).exit_code == 0
    assert task_of(env).current_stage == "coder"


# --------------------------------------------------------------------------- #
# AC5 / AC8
# --------------------------------------------------------------------------- #


def test_accept_is_not_how_this_stage_finishes(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC5's neighbour: the gate is a property of the call site (LF-7 D4)."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    write_tests(workspace_for_tests(env), criteria=len(TWO_CRITERIA))

    refused = runner.invoke(cli, ["accept", "--as-is"])
    assert refused.exit_code != 0
    assert "no person approves" in refused.stderr

    task = task_of(env)
    assert task.plan_accepted is True  # the Architect's approval, untouched
    assert task.state == STATE_AWAITING_ARTIFACT


def test_the_contract_does_not_know_what_language_this_is(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC8: every judgement lands on a path or on the table's shape."""
    env, _ = at_the_tests_stage(runner, configured, tracker)
    write_tests(
        workspace_for_tests(env),
        path="internal/auth/reset_test.go",
        body="package auth\n\nfunc TestLinkStillValid(t *testing.T) {}\n",
        coverage=[
            {"ac_id": "AC1", "test_ids": ["internal/auth/reset_test.go::TestLinkStillValid"]},
            {"ac_id": "AC2", "test_ids": ["internal/auth/reset_test.go::TestLinkExpired"]},
        ],
    )

    result = runner.invoke(cli, ["promote"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert task_of(env).current_stage == "coder"


# --------------------------------------------------------------------------- #
# AC7: adding the stage did not move the state machine
# --------------------------------------------------------------------------- #


def test_the_sequence_grew_and_the_states_did_not() -> None:
    """AC7: LF-7's promise, checked from the other side now that it was used."""
    from lazyfish.authority import CALL_SITE_ARCHITECT, CALL_SITE_TESTER_WRITE
    from lazyfish.db import LEGAL_TRANSITIONS
    from lazyfish.orchestrator import STAGE_SEQUENCE

    assert STAGE_SEQUENCE[:2] == (CALL_SITE_ARCHITECT, CALL_SITE_TESTER_WRITE)
    # The seven states LF-7 settled on, unchanged by a stage being added.
    assert len(LEGAL_TRANSITIONS) == 7
