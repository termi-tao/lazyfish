"""The last two stages: the implementation, and the report on what the tests do.

Between them they complete the Tester -- `tester@write` writes the tests before
there is anything to run them against, `tester@verify` runs them once there is.
The Coder sits in between because that is the order ruling 2 settled on.

This is also where the core executes something for the first time. It runs the
command the profile names and reads its exit code; it does not parse output and
does not know what framework produced it.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from lazyfish.artifacts import (
    RULE_OUTCOME_KNOWN,
    RULE_OUTCOME_MATCHES_RUN,
    RULE_PATCH_NOT_EMPTY,
)
from lazyfish.cli import cli
from lazyfish.db import STATE_COMPLETED
from lazyfish.workspace import materialize

from .conftest import (
    FakeTracker,
    git,
    open_database,
    promote,
    stage_workspace,
    stored_content,
    task_of,
    workspace_for_tests,
    write_config,
    write_credentials,
    write_implementation,
    write_plan,
    write_report,
    write_tests,
)

TWO_CRITERIA = [
    "A link generated now is still accepted 23 hours later.",
    "A link older than the window is refused.",
]

TEST_BODY = (
    "def test_ttl():\n"
    "    from src.auth.reset_token import RESET_TOKEN_TTL\n"
    "\n"
    "    assert RESET_TOKEN_TTL == 86400\n"
)


def at_the_coder_stage(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> dict[str, Path]:
    """prep, plan, accept, tests -- leaving the ticket at `coder`."""
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    write_plan(Path(result.stdout.strip().splitlines()[-1][3:]), acceptance_criteria=TWO_CRITERIA)
    assert runner.invoke(cli, ["accept", "--as-is"]).exit_code == 0

    write_tests(
        workspace_for_tests(configured),
        path="tests/test_reset.py",
        body=TEST_BODY,
        criteria=len(TWO_CRITERIA),
    )
    promote(runner)
    return configured


def at_the_verify_stage(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> dict[str, Path]:
    env = at_the_coder_stage(runner, configured, tracker)
    write_implementation(stage_workspace(env, "coder"))
    promote(runner)
    return env


def artifact_of(env: dict[str, Path], type_name: str) -> tuple[object, dict]:
    with open_database(env) as database:
        task = database.get_by_ticket("PROJ-1", "work")
        assert task is not None
        found = [a for a in database.list_artifacts(task.id) if a.type == type_name]
        assert found, f"no {type_name} recorded"
        artifact = found[-1]
    return artifact, stored_content(env, task.id, artifact.id)


# --------------------------------------------------------------------------- #
# AC1: four stages end to end
# --------------------------------------------------------------------------- #


def test_the_whole_pipeline_runs(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC1: plan, tests, implementation, report."""
    env = at_the_verify_stage(runner, configured, tracker)
    assert task_of(env).current_stage == "tester@verify"

    write_report(stage_workspace(env, "tester@verify"))
    result = promote(runner)
    assert "TestReport" in result.stdout

    task = task_of(env)
    assert task.state == STATE_COMPLETED
    with open_database(env) as database:
        types = {a.type for a in database.list_artifacts(task.id) if a.promoted_at}
    assert types == {"TechnicalPlan", "TestArtifact", "ImplementationPatch", "TestReport"}


# --------------------------------------------------------------------------- #
# AC2 / AC3: what the Coder's patch contains, and what survives it
# --------------------------------------------------------------------------- #


def test_the_coder_patch_does_not_repeat_the_tests(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC2: its workspace opened with the tests already in it (LF-9 D1)."""
    env = at_the_coder_stage(runner, configured, tracker)
    write_implementation(stage_workspace(env, "coder"))
    promote(runner)

    artifact, content = artifact_of(env, "ImplementationPatch")
    assert content["files"] == ["src/auth/reset_token.py"]
    assert "tests/test_reset.py" not in content["patch"]
    # And it records what it was built on.
    assert content["applied_artifacts"]


def test_the_coder_cannot_rewrite_the_promoted_tests(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC3: LF-6 AC1, finally reachable now that a Coder exists to violate it."""
    env = at_the_coder_stage(runner, configured, tracker)
    workspace = stage_workspace(env, "coder")
    write_implementation(workspace)
    # The oldest trick: make the test agree with the code instead.
    (workspace / "tests" / "test_reset.py").write_text(
        "def test_ttl():\n    assert True\n", encoding="utf-8"
    )
    promote(runner)

    with open_database(env) as database:
        task = database.get_by_ticket("PROJ-1", "work")
    assert task is not None
    verify = materialize(task, "tester@verify")

    _, promoted_tests = artifact_of(env, "TestArtifact")
    assert (verify.path / "tests" / "test_reset.py").read_text(encoding="utf-8") == TEST_BODY
    assert "assert True" not in (verify.path / "tests" / "test_reset.py").read_text(
        encoding="utf-8"
    )
    # The edit is still recorded against the Coder, it simply does not travel.
    _, implementation = artifact_of(env, "ImplementationPatch")
    assert "tests/test_reset.py" in implementation["files"]


def test_an_empty_implementation_is_refused(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    env = at_the_coder_stage(runner, configured, tracker)
    result = runner.invoke(cli, ["promote"])
    assert result.exit_code == 6
    _, rejection = artifact_of(env, "Rejection")
    assert RULE_PATCH_NOT_EMPTY in [finding["rule"] for finding in rejection["findings"]]


# --------------------------------------------------------------------------- #
# AC4 / AC5 / AC6 / AC7: the report against a real run
# --------------------------------------------------------------------------- #


def test_a_report_claiming_green_while_the_suite_fails_is_refused(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker, repo: Path
) -> None:
    """AC4: the whole reason lazyfish runs the command itself (LF-9 D4)."""
    write_config(
        configured["config"],
        profiles={"work": {"repo": str(repo), "test_command": "sh -c 'exit 1'"}},
    )
    write_credentials(configured["credentials"])
    env = at_the_verify_stage(runner, configured, tracker)

    write_report(stage_workspace(env, "tester@verify"), outcome="GREEN")
    result = runner.invoke(cli, ["promote"])
    assert result.exit_code == 6

    _, rejection = artifact_of(env, "Rejection")
    assert RULE_OUTCOME_MATCHES_RUN in [finding["rule"] for finding in rejection["findings"]]


def test_a_report_claiming_red_while_the_suite_passes_is_refused(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC4, the other direction. A false RED is a lie about the same fact."""
    env = at_the_verify_stage(runner, configured, tracker)  # test_command exits 0

    write_report(stage_workspace(env, "tester@verify"), outcome="RED")
    assert runner.invoke(cli, ["promote"]).exit_code == 6

    _, rejection = artifact_of(env, "Rejection")
    assert RULE_OUTCOME_MATCHES_RUN in [finding["rule"] for finding in rejection["findings"]]


def test_a_red_report_that_matches_the_run_completes_the_ticket(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker, repo: Path
) -> None:
    """AC6: this stage reports, it does not judge (LF-9 D6)."""
    write_config(
        configured["config"],
        profiles={"work": {"repo": str(repo), "test_command": "sh -c 'exit 1'"}},
    )
    write_credentials(configured["credentials"])
    env = at_the_verify_stage(runner, configured, tracker)

    write_report(
        stage_workspace(env, "tester@verify"),
        outcome="RED",
        tests=[{"id": "tests/test_reset.py::test_ttl", "status": "failed"}],
    )
    promote(runner)

    assert task_of(env).state == STATE_COMPLETED
    _, report = artifact_of(env, "TestReport")
    assert report["outcome"] == "RED"
    assert report["test_exit_code"] == 1


def test_an_unknown_outcome_is_refused(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    env = at_the_verify_stage(runner, configured, tracker)
    write_report(stage_workspace(env, "tester@verify"), outcome="probably fine")
    assert runner.invoke(cli, ["promote"]).exit_code == 6

    _, rejection = artifact_of(env, "Rejection")
    assert RULE_OUTCOME_KNOWN in [finding["rule"] for finding in rejection["findings"]]


def test_without_a_test_command_the_report_cannot_be_checked(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker, repo: Path
) -> None:
    """AC5: refused, naming the profile and the key, and not as a traceback."""
    env = at_the_verify_stage(runner, configured, tracker)
    write_config(configured["config"], profiles={"work": {"repo": str(repo)}})

    write_report(stage_workspace(env, "tester@verify"))
    result = runner.invoke(cli, ["promote"])
    assert result.exit_code != 0
    assert "test_command" in result.stderr
    assert "profile.work" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_core_does_not_know_what_ran(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker, repo: Path
) -> None:
    """AC7: a command that is not a test runner at all still works."""
    write_config(
        configured["config"],
        profiles={"work": {"repo": str(repo), "test_command": "sh -c 'echo not a test runner'"}},
    )
    write_credentials(configured["credentials"])
    env = at_the_verify_stage(runner, configured, tracker)

    write_report(stage_workspace(env, "tester@verify"), outcome="GREEN")
    promote(runner)
    assert task_of(env).state == STATE_COMPLETED


def test_the_report_records_what_it_judged(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    env = at_the_verify_stage(runner, configured, tracker)
    write_report(stage_workspace(env, "tester@verify"))
    promote(runner)

    _, report = artifact_of(env, "TestReport")
    _, tests = artifact_of(env, "TestArtifact")
    assert len(report["applied_artifacts"]) == 2


# --------------------------------------------------------------------------- #
# AC8 and the invariants around the new execution path
# --------------------------------------------------------------------------- #


def test_adding_two_stages_did_not_move_the_state_machine() -> None:
    """AC8: LF-7's promise, now measured across four stages instead of one."""
    from lazyfish.db import LEGAL_TRANSITIONS
    from lazyfish.orchestrator import STAGE_SEQUENCE

    assert len(STAGE_SEQUENCE) == 4
    assert len(LEGAL_TRANSITIONS) == 7


def test_the_ticket_branch_never_moves(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker, repo: Path
) -> None:
    """R2: materialisation commits inside a workspace; nothing may follow it out."""
    env = at_the_coder_stage(runner, configured, tracker)
    before = git(repo, "rev-parse", "lazyfish/PROJ-1").strip()

    write_implementation(stage_workspace(env, "coder"))
    promote(runner)
    write_report(stage_workspace(env, "tester@verify"))
    promote(runner)

    assert git(repo, "rev-parse", "lazyfish/PROJ-1").strip() == before


def test_abandon_removes_every_stage_directory(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """R3: four workspaces per ticket now, and cleanup must not know how many."""
    env = at_the_verify_stage(runner, configured, tracker)
    root = stage_workspace(env, "coder").parent
    assert {path.name for path in root.iterdir()} >= {"architect", "tester@write", "coder"}

    assert runner.invoke(cli, ["abandon", "--yes"]).exit_code == 0
    assert not root.exists()


def test_the_test_command_cannot_see_lazyfish_state(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker, repo: Path
) -> None:
    """R1: a suite that can read the tool's data directory is a loop nobody wants."""
    write_config(
        configured["config"],
        profiles={
            "work": {
                "repo": str(repo),
                "test_command": "sh -c '[ -z \"$LAZYFISH_DATA_DIR\" ]'",
            }
        },
    )
    write_credentials(configured["credentials"])
    env = at_the_verify_stage(runner, configured, tracker)

    write_report(stage_workspace(env, "tester@verify"), outcome="GREEN")
    promote(runner)
    assert task_of(env).state == STATE_COMPLETED
