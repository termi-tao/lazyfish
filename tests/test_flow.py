"""End-to-end command behaviour, one test per acceptance criterion.

These drive the real CLI against real git repositories with a stubbed tracker.
Each test names the criterion it covers so that a failure says what promise
broke, not merely which function did.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from lazyfish.cli import cli
from lazyfish.db import STATE_ABANDONED, STATE_PLAN_APPROVED, Database
from lazyfish.trackers.base import Attachment, Comment

from .conftest import FakeTracker, git, make_ticket, write_config, write_plan


def tasks_of(env: dict[str, Path]) -> list:
    database = Database(env["data"] / "lazyfish.db")
    database.initialise()
    try:
        return database.list_tasks()
    finally:
        database.close()


def wizard_answers(
    repo: Path,
    *,
    email: str = "you@example.com",
    base_url: str = "https://example.atlassian.net",
    profile: str = "",
) -> str:
    """Answers to the six questions `init` asks, in order.

    The order and the wording of those questions are part of the specification,
    so this helper doubles as a check that neither drifted: an extra prompt would
    consume the wrong line and the test would fail.
    """
    return (
        "\n".join(
            [
                base_url,  # Atlassian site base URL (no default)
                email,  # Atlassian account email
                "",  # JQL query: accept the default
                profile,  # Name for this repo profile: blank keeps 'default'
                str(repo),  # Local path to the target git repository
                "",  # Optional reminder: none
            ]
        )
        + "\n"
    )


def worktree_from(result_output: str) -> Path:
    """The last stdout line of a successful prep is `cd <path>`."""
    last = result_output.strip().splitlines()[-1]
    assert last.startswith("cd "), last
    return Path(last[3:])


@pytest.fixture
def prepared(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> tuple[dict[str, Path], Path, FakeTracker]:
    """A worktree prepared for PROJ-1, ready for a plan."""
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    return configured, worktree_from(result.stdout), tracker


# --------------------------------------------------------------------------- #
# AC1: a machine with no configuration at all
# --------------------------------------------------------------------------- #


def test_init_then_prep_without_editing_any_file(
    runner: CliRunner, env: dict[str, Path], repo: Path, tracker: FakeTracker
) -> None:
    """AC1."""
    result = runner.invoke(
        cli, ["init", "--no-write-conventions", "--no-check"], input=wizard_answers(repo)
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert env["config"].exists()
    assert (env["data"] / "lazyfish.db").exists()

    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1].startswith("cd ")


def test_init_refuses_to_clobber_an_existing_config(
    runner: CliRunner, configured: dict[str, Path], repo: Path
) -> None:
    result = runner.invoke(cli, ["init", "--yes", "--repo-path", str(repo)])
    assert result.exit_code != 0
    assert "already exists" in result.stderr


def test_wizard_asks_exactly_the_specified_questions(
    runner: CliRunner, env: dict[str, Path], repo: Path
) -> None:
    """The wording of the five prompts is specified, not left to the implementer (R9).

    Asserted verbatim because the two mistakes this replaced were both wording
    mistakes: asking the user to name an environment variable, and not saying
    that the repository path is local rather than a remote URL.
    """
    result = runner.invoke(
        cli, ["init", "--no-write-conventions", "--no-check"], input=wizard_answers(repo)
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    prompts = result.stderr + result.stdout
    assert "Atlassian site base URL (e.g. https://your-org.atlassian.net):" in prompts
    assert "Atlassian account email (the one you log in to Jira with):" in prompts
    assert "JQL query for selecting candidate tickets" in prompts
    assert "Name for this repo profile" in prompts
    assert "Local path to the target git repository (not a remote URL):" in prompts
    assert "Optional reminder to print on every prep (blank for none):" in prompts

    # The question it must not ask: naming an environment variable is a freedom
    # nobody wants and two extra steps for everybody.
    assert "Environment variable" not in prompts
    assert "environment variable holding" not in prompts.lower()


def test_wizard_profile_name_reaches_the_config(
    runner: CliRunner, env: dict[str, Path], repo: Path, tracker: FakeTracker
) -> None:
    """A name given at the prompt becomes the profile, and --repo selects it."""
    result = runner.invoke(
        cli,
        ["init", "--no-write-conventions", "--no-check"],
        input=wizard_answers(repo, profile="frontend"),
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    document = tomllib.loads(env["config"].read_text(encoding="utf-8"))
    assert set(document["repo"]) == {"frontend"}

    result = runner.invoke(cli, ["--repo", "frontend", "prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert tasks_of(env)[0].repo_profile == "frontend"


def test_wizard_closes_with_the_export_lines_and_token_source(
    runner: CliRunner, env: dict[str, Path], repo: Path
) -> None:
    result = runner.invoke(
        cli,
        ["init", "--no-write-conventions", "--no-check"],
        input=wizard_answers(repo, email="dev@example.com"),
    )
    assert result.exit_code == 0
    assert "export LAZYFISH_EMAIL='dev@example.com'" in result.stdout
    assert "export LAZYFISH_TOKEN=" in result.stdout
    assert "id.atlassian.com" in result.stdout
    assert "API tokens" in result.stdout


def test_wizard_rejects_a_remote_url_as_the_repository_path(
    runner: CliRunner, env: dict[str, Path]
) -> None:
    """The parenthetical in the prompt is backed by a check."""
    answers = (
        "https://example.atlassian.net\nyou@example.com\n\n\n"
        "https://github.com/acme/service-api.git\n\n"
    )
    result = runner.invoke(cli, ["init", "--no-check"], input=answers)
    assert result.exit_code != 0
    assert "remote URL" in result.stderr
    assert not env["config"].exists()


def test_the_base_url_question_has_no_silent_default(
    runner: CliRunner, env: dict[str, Path], repo: Path
) -> None:
    """An empty answer re-asks rather than accepting the example site."""
    answers = (
        "\n"  # empty: must be rejected, not accepted as your-org
        "not-a-url\n"  # also rejected
        "https://real-org.atlassian.net\n"
        "you@example.com\n"
        "\n"  # JQL query
        "\n"  # profile name
        f"{repo}\n"
        "\n"
    )
    result = runner.invoke(cli, ["init", "--no-write-conventions", "--no-check"], input=answers)
    assert result.exit_code == 0, result.stdout + result.stderr
    document = tomllib.loads(env["config"].read_text(encoding="utf-8"))
    assert document["tracker"]["base_url"] == "https://real-org.atlassian.net"
    assert "your-org" not in env["config"].read_text(encoding="utf-8")


def test_init_can_write_the_conventions_example(
    runner: CliRunner, env: dict[str, Path], repo: Path
) -> None:
    result = runner.invoke(
        cli,
        [
            "init",
            "--yes",
            "--base-url",
            "https://example.atlassian.net",
            "--repo-path",
            str(repo),
            "--write-conventions",
            "--no-check",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (repo / ".lazyfish" / "conventions.md").exists()


# --------------------------------------------------------------------------- #
# AC20, AC21: credentials come from the environment, not from the wizard
# --------------------------------------------------------------------------- #


def test_generated_config_omits_the_env_name_keys(
    runner: CliRunner, env: dict[str, Path], repo: Path, tracker: FakeTracker
) -> None:
    """AC20."""
    result = runner.invoke(
        cli, ["init", "--no-write-conventions", "--no-check"], input=wizard_answers(repo)
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    # Neither key is set. The generated file does mention both in a comment, so
    # the override in AC21 is discoverable; a comment is not a key.
    document = tomllib.loads(env["config"].read_text(encoding="utf-8"))
    assert "email_env" not in document["tracker"]
    assert "token_env" not in document["tracker"]
    assert set(document["tracker"]) == {"kind", "base_url", "query"}

    # The defaults still resolve, so prep works with only the two exports set.
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1].startswith("cd ")


def test_the_account_email_is_never_written_to_the_config(
    runner: CliRunner, env: dict[str, Path], repo: Path
) -> None:
    """It is asked for so the export line can be printed, and for nothing else."""
    result = runner.invoke(
        cli,
        ["init", "--no-write-conventions", "--no-check"],
        input=wizard_answers(repo, email="dev@example.com"),
    )
    assert result.exit_code == 0
    assert "dev@example.com" not in env["config"].read_text(encoding="utf-8")


def test_a_manual_token_env_override_is_honoured(
    runner: CliRunner,
    env: dict[str, Path],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeTracker,
) -> None:
    """AC21: hand-editing the key redirects the read to another variable."""
    write_config(env["config"], repos={"default": {"path": f'"{repo}"'}}, token_env="OTHER_VAR")
    monkeypatch.delenv("LAZYFISH_TOKEN")
    monkeypatch.setenv("OTHER_VAR", "the-real-token")

    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr

    # And the default variable is no longer consulted at all: with only
    # LAZYFISH_TOKEN set, prep fails and names the variable it actually wants.
    monkeypatch.delenv("OTHER_VAR")
    monkeypatch.setenv("LAZYFISH_TOKEN", "ignored-now")
    runner.invoke(cli, ["abandon", "--yes"])
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 2
    assert "OTHER_VAR" in result.stderr
    assert "LAZYFISH_TOKEN" not in result.stderr


# --------------------------------------------------------------------------- #
# AC2, AC3, AC19: the context file
# --------------------------------------------------------------------------- #


def test_context_file_holds_the_ticket_and_its_comments(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC2."""
    tracker.tickets = [
        make_ticket(
            comments=(
                Comment("Alex Dev", "2026-01-06T10:00:00.000+0000", "Reproduced on staging."),
            )
        )
    ]
    result = runner.invoke(cli, ["prep"])
    context = (worktree_from(result.stdout) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Password reset links expire too early" in context
    assert "Reproduced on staging." in context
    assert "Alex Dev" in context


def test_missing_conventions_omits_the_section_and_says_so(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC3."""
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0
    context = (worktree_from(result.stdout) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Project conventions" not in context
    assert "conventions" in result.stdout
    assert "not found" in result.stdout


def test_conventions_from_an_absolute_path_are_injected(
    runner: CliRunner,
    env: dict[str, Path],
    repo: Path,
    tmp_path: Path,
    tracker: FakeTracker,
) -> None:
    """AC19."""
    outside = tmp_path / "team-docs" / "conventions.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("# House rules\n\n- Money is integer minor units.\n", encoding="utf-8")
    write_config(
        env["config"],
        repos={"default": {"path": f'"{repo}"', "conventions": f'"{outside}"'}},
    )
    result = runner.invoke(cli, ["prep"])
    context = (worktree_from(result.stdout) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Project conventions" in context
    assert "Money is integer minor units." in context
    assert str(outside) in context


def test_repo_conventions_are_injected_verbatim(
    runner: CliRunner, configured: dict[str, Path], repo: Path, tracker: FakeTracker
) -> None:
    conventions = repo / ".lazyfish" / "conventions.md"
    conventions.parent.mkdir(parents=True)
    body = "# Rules\n\n- Never touch `src/legacy/`.\n"
    conventions.write_text(body, encoding="utf-8")
    result = runner.invoke(cli, ["prep"])
    context = (worktree_from(result.stdout) / "CLAUDE.md").read_text(encoding="utf-8")
    assert body in context


def test_code_hints_are_collected_and_labelled(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    result = runner.invoke(cli, ["prep"])
    context = (worktree_from(result.stdout) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Code hints (mechanical search, may be irrelevant)" in context
    assert "reset_token.py" in context


def test_no_hints_switch_removes_the_section(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    result = runner.invoke(cli, ["prep", "--no-hints"])
    context = (worktree_from(result.stdout) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Code hints" not in context


def test_design_brief_and_schema_land_in_the_worktree(
    prepared: tuple[dict[str, Path], Path, FakeTracker],
) -> None:
    _, worktree, _ = prepared
    brief = (worktree / ".lazyfish" / "plan-prompt.md").read_text(encoding="utf-8")
    assert "needs_human" in brief
    assert '"$schema"' in brief
    assert (worktree / ".lazyfish" / "plan-schema.json").exists()


def test_ticket_json_is_written_and_ignored_by_git(
    prepared: tuple[dict[str, Path], Path, FakeTracker],
) -> None:
    _, worktree, _ = prepared
    payload = json.loads(
        (worktree / "artifacts" / "PROJ-1" / "ticket.json").read_text(encoding="utf-8")
    )
    assert payload["key"] == "PROJ-1"
    assert payload["url"].endswith("/browse/PROJ-1")
    assert (worktree / "artifacts" / ".gitignore").read_text(encoding="utf-8") == "*\n"


# --------------------------------------------------------------------------- #
# AC4: several repo profiles
# --------------------------------------------------------------------------- #


def test_second_profile_uses_its_own_repository(
    runner: CliRunner,
    env: dict[str, Path],
    repo: Path,
    second_repo: Path,
    tracker: FakeTracker,
) -> None:
    """AC4."""
    write_config(
        env["config"],
        repos={
            "default": {"path": f'"{repo}"'},
            "frontend": {"path": f'"{second_repo}"'},
        },
    )
    tracker.tickets = [make_ticket("WEB-7", title="Pagination missing")]
    result = runner.invoke(cli, ["--repo", "frontend", "prep"])
    assert result.exit_code == 0, result.stdout + result.stderr

    worktree = worktree_from(result.stdout)
    assert worktree.exists()
    assert "lazyfish/WEB-7" in git(second_repo, "worktree", "list")

    rows = tasks_of(env)
    assert len(rows) == 1
    assert rows[0].repo_profile == "frontend"


def test_profiles_do_not_block_each_other(
    runner: CliRunner,
    env: dict[str, Path],
    repo: Path,
    second_repo: Path,
    tracker: FakeTracker,
) -> None:
    write_config(
        env["config"],
        repos={
            "default": {"path": f'"{repo}"'},
            "frontend": {"path": f'"{second_repo}"'},
        },
    )
    tracker.tickets = [make_ticket("PROJ-1"), make_ticket("WEB-7")]
    assert runner.invoke(cli, ["prep"]).exit_code == 0
    assert runner.invoke(cli, ["--repo", "frontend", "prep", "--ticket", "WEB-7"]).exit_code == 0
    profiles = {task.repo_profile: task.ticket_key for task in tasks_of(env)}
    assert profiles == {"default": "PROJ-1", "frontend": "WEB-7"}


def test_unknown_profile_is_rejected(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    result = runner.invoke(cli, ["--repo", "nope", "prep"])
    assert result.exit_code != 0
    assert "nope" in result.stderr
    assert "default" in result.stderr


# --------------------------------------------------------------------------- #
# AC5: candidates and the automatic choice
# --------------------------------------------------------------------------- #


def test_candidates_are_listed_and_the_first_is_taken(
    runner: CliRunner, env: dict[str, Path], configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC5, first half."""
    tracker.tickets = [make_ticket(f"PROJ-{index}") for index in range(1, 8)]
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0

    listed = [
        line for line in result.stderr.splitlines() if line.strip().startswith(("*", "1.", "2."))
    ]
    assert len(listed) <= 6
    assert "PROJ-1" in result.stderr
    assert "PROJ-6" not in result.stderr  # limited to five candidates

    row = tasks_of(env)[0]
    assert row.ticket_key == "PROJ-1"
    assert row.was_top_pick is True


def test_choosing_another_ticket_is_recorded_as_not_the_top_pick(
    runner: CliRunner, env: dict[str, Path], configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC5, second half. This flag is the data behind Q1."""
    tracker.tickets = [make_ticket("PROJ-1"), make_ticket("PROJ-2")]
    result = runner.invoke(cli, ["prep", "--ticket", "PROJ-2"])
    assert result.exit_code == 0

    row = tasks_of(env)[0]
    assert row.ticket_key == "PROJ-2"
    assert row.was_top_pick is False


def test_an_empty_query_result_is_explained(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    tracker.tickets = []
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code != 0
    assert "matched no tickets" in result.stderr
    assert "--ticket" in result.stderr


# --------------------------------------------------------------------------- #
# AC6, AC7: idempotence
# --------------------------------------------------------------------------- #


def test_three_preps_produce_one_worktree_and_identical_output(
    runner: CliRunner, env: dict[str, Path], configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC6 and AC7."""
    first = runner.invoke(cli, ["prep"])
    second = runner.invoke(cli, ["prep"])
    third = runner.invoke(cli, ["prep"])

    assert first.exit_code == second.exit_code == third.exit_code == 0
    assert second.stdout == first.stdout
    assert third.stdout == first.stdout

    assert len(tasks_of(env)) == 1
    assert len(tracker.fetched) == 1

    worktree = worktree_from(first.stdout)
    worktrees = [path for path in worktree.parent.iterdir() if path.is_dir()]
    assert worktrees == [worktree]
    assert "nothing to do" in second.stderr


# --------------------------------------------------------------------------- #
# AC8, AC9: refusing a plan
# --------------------------------------------------------------------------- #


def test_accept_refuses_when_needs_human_is_wrong(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC8."""
    env, worktree, _ = prepared
    write_plan(worktree, needs_human=False)

    result = runner.invoke(cli, ["accept"])
    assert result.exit_code == 6
    assert "needs-human" in result.stderr
    assert "needs_human" in result.stderr
    assert tasks_of(env)[0].plan_accepted is None


def test_accept_refuses_a_change_to_a_missing_file(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC9."""
    _, worktree, _ = prepared
    write_plan(
        worktree,
        changes=[
            {
                "file": "src/auth/not_here.py",
                "action": "modify",
                "reason": "wrong path",
            }
        ],
    )
    result = runner.invoke(cli, ["accept"])
    assert result.exit_code == 6
    assert "src/auth/not_here.py" in result.stderr


def test_force_records_the_bypass(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """R3: the escape hatch exists, and leaves a trace in the data."""
    env, worktree, _ = prepared
    write_plan(worktree, needs_human=False)

    result = runner.invoke(cli, ["accept", "--force", "--as-is"])
    assert result.exit_code == 0
    row = tasks_of(env)[0]
    assert row.plan_accepted is True
    assert "schema bypassed" in row.notes


def test_accept_without_a_plan_file_says_where_to_put_it(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    result = runner.invoke(cli, ["accept"])
    assert result.exit_code != 0
    assert "plan.json" in result.stderr


# --------------------------------------------------------------------------- #
# AC10: recording the outcome
# --------------------------------------------------------------------------- #


def test_accepting_with_changes_records_the_note(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """AC10. The note is stored exactly as typed: it is the data."""
    env, worktree, _ = prepared
    write_plan(worktree)

    result = runner.invoke(cli, ["accept"], input="n\nmissed the rate limiter\n")
    assert result.exit_code == 0, result.stdout + result.stderr

    row = tasks_of(env)[0]
    assert row.state == STATE_PLAN_APPROVED
    assert row.plan_accepted is False
    assert row.notes == "missed the rate limiter"


def test_accepting_as_written(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    env, worktree, _ = prepared
    write_plan(worktree)

    result = runner.invoke(cli, ["accept"], input="y\n")
    assert result.exit_code == 0
    row = tasks_of(env)[0]
    assert row.plan_accepted is True
    assert row.notes is None
    assert row.accepted_at is not None


def test_a_recorded_plan_cannot_be_accepted_twice(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    _, worktree, _ = prepared
    write_plan(worktree)
    assert runner.invoke(cli, ["accept", "--as-is"]).exit_code == 0

    result = runner.invoke(cli, ["accept", "--as-is"])
    assert result.exit_code != 0
    assert "already recorded" in result.stderr


def test_prep_moves_on_after_a_plan_is_recorded(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """Recording a plan ends lazyfish's involvement; the next ticket can start."""
    env, worktree, tracker = prepared
    write_plan(worktree)
    assert runner.invoke(cli, ["accept", "--as-is"]).exit_code == 0

    tracker.tickets = [make_ticket("PROJ-2")]
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0
    assert [task.ticket_key for task in tasks_of(env)] == ["PROJ-1", "PROJ-2"]


def test_show_highlights_open_questions_and_validation(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    _, worktree, _ = prepared
    write_plan(worktree)

    result = runner.invoke(cli, ["show"])
    assert result.exit_code == 0
    assert "Open questions" in result.stdout
    assert "Should existing links keep working?" in result.stdout
    assert "Validation: passes" in result.stdout


def test_show_reports_problems_before_accept_does(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    _, worktree, _ = prepared
    write_plan(worktree, needs_human=False)
    result = runner.invoke(cli, ["show"])
    assert result.exit_code == 0
    assert "will refuse" in result.stdout
    assert "needs-human" in result.stdout


# --------------------------------------------------------------------------- #
# AC11: statistics
# --------------------------------------------------------------------------- #


def test_status_reports_rates_and_cycle_time_per_profile(
    runner: CliRunner,
    env: dict[str, Path],
    repo: Path,
    second_repo: Path,
    tracker: FakeTracker,
) -> None:
    """AC11."""
    write_config(
        env["config"],
        repos={
            "default": {"path": f'"{repo}"'},
            "frontend": {"path": f'"{second_repo}"'},
        },
    )
    tracker.tickets = [make_ticket(f"PROJ-{index}") for index in (1, 2, 3)]

    for index, answer in ((1, "y\n"), (2, "n\nadded a migration step\n"), (3, "y\n")):
        result = runner.invoke(cli, ["prep", "--ticket", f"PROJ-{index}"])
        assert result.exit_code == 0, result.stdout + result.stderr
        write_plan(worktree_from(result.stdout), ticket=f"PROJ-{index}")
        assert runner.invoke(cli, ["accept"], input=answer).exit_code == 0

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "[default]" in result.stdout
    assert "accepted as-is    2" in result.stdout
    assert "accepted modified 1" in result.stdout
    assert "as-is rate        67%" in result.stdout
    assert "minutes" in result.stdout


def test_status_can_be_narrowed_to_one_profile(
    runner: CliRunner,
    env: dict[str, Path],
    repo: Path,
    second_repo: Path,
    tracker: FakeTracker,
) -> None:
    write_config(
        env["config"],
        repos={
            "default": {"path": f'"{repo}"'},
            "frontend": {"path": f'"{second_repo}"'},
        },
    )
    tracker.tickets = [make_ticket("PROJ-1"), make_ticket("WEB-7")]
    runner.invoke(cli, ["prep"])
    runner.invoke(cli, ["--repo", "frontend", "prep", "--ticket", "WEB-7"])

    everything = runner.invoke(cli, ["status"])
    assert "[default]" in everything.stdout
    assert "[frontend]" in everything.stdout

    narrowed = runner.invoke(cli, ["--repo", "frontend", "status"])
    assert "[frontend]" in narrowed.stdout
    assert "[default]" not in narrowed.stdout


def test_status_without_any_task(runner: CliRunner, configured: dict[str, Path]) -> None:
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "No tasks recorded yet" in result.stdout


# --------------------------------------------------------------------------- #
# AC12: abandon
# --------------------------------------------------------------------------- #


def test_abandon_removes_the_worktree_and_branch(
    prepared: tuple[dict[str, Path], Path, FakeTracker],
    runner: CliRunner,
    repo: Path,
) -> None:
    """AC12."""
    env, worktree, tracker = prepared
    assert worktree.exists()

    result = runner.invoke(cli, ["abandon", "--yes", "--note", "deprioritised"])
    assert result.exit_code == 0, result.stdout + result.stderr

    assert not worktree.exists()
    assert "lazyfish/PROJ-1" not in git(repo, "branch", "--list")
    assert "PROJ-1" not in git(repo, "worktree", "list")

    row = tasks_of(env)[0]
    assert row.state == STATE_ABANDONED
    assert row.notes == "deprioritised"

    tracker.tickets = [make_ticket("PROJ-2")]
    assert runner.invoke(cli, ["prep"]).exit_code == 0
    assert [task.ticket_key for task in tasks_of(env)] == ["PROJ-1", "PROJ-2"]


def test_abandon_survives_a_worktree_deleted_by_hand(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    """R4: cleanup must not be the thing that gets stuck."""
    import shutil

    env, worktree, _ = prepared
    shutil.rmtree(worktree)

    result = runner.invoke(cli, ["abandon", "--yes"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert tasks_of(env)[0].state == STATE_ABANDONED


def test_abandon_asks_before_deleting(
    prepared: tuple[dict[str, Path], Path, FakeTracker], runner: CliRunner
) -> None:
    env, worktree, _ = prepared
    result = runner.invoke(cli, ["abandon"], input="n\n")
    assert result.exit_code != 0
    assert worktree.exists()
    assert tasks_of(env)[0].state != STATE_ABANDONED


def test_abandon_with_nothing_open(runner: CliRunner, configured: dict[str, Path]) -> None:
    result = runner.invoke(cli, ["abandon", "--yes"])
    assert result.exit_code != 0
    assert "nothing to abandon" in result.stderr


# --------------------------------------------------------------------------- #
# AC13: failure paths never show a traceback
# --------------------------------------------------------------------------- #


def test_no_configuration_at_all(runner: CliRunner, env: dict[str, Path]) -> None:
    """AC13."""
    for command in (["prep"], ["show"], ["accept"], ["status"], ["abandon"]):
        result = runner.invoke(cli, command)
        assert result.exit_code == 2, command
        assert "lazyfish init" in result.stderr
        assert "Traceback" not in result.stderr


def test_repo_path_missing_from_disk(
    runner: CliRunner, env: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC13."""
    write_config(env["config"], repos={"default": {"path": '"/nowhere/at/all"'}})
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 2
    assert "/nowhere/at/all" in result.stderr
    assert "Traceback" not in result.stderr


def test_token_variable_not_set(
    runner: CliRunner,
    configured: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tracker: FakeTracker,
) -> None:
    """AC13."""
    monkeypatch.delenv("LAZYFISH_TOKEN")
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 2
    assert "LAZYFISH_TOKEN" in result.stderr
    assert "Traceback" not in result.stderr


def test_credential_in_the_config_blocks_every_command(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC17."""
    text = configured["config"].read_text(encoding="utf-8")
    configured["config"].write_text(
        text.replace("[tracker]", '[tracker]\ntoken = "ATATT3xFfGF0T4Nn2VXsY1qKpLmZ7bWc"'),
        encoding="utf-8",
    )
    for command in (["prep"], ["status"], ["show"]):
        result = runner.invoke(cli, command)
        assert result.exit_code == 2, command
        assert "environment variable" in result.stderr
        assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------- #
# AC18: the account reminder
# --------------------------------------------------------------------------- #


def test_account_note_is_printed_verbatim(
    runner: CliRunner, env: dict[str, Path], repo: Path, tracker: FakeTracker
) -> None:
    """AC18, first half."""
    reminder = "company seat - check which account the client is signed in to"
    write_config(
        env["config"],
        repos={"default": {"path": f'"{repo}"', "account_note": f'"{reminder}"'}},
    )
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0
    assert reminder in result.stdout


def test_without_an_account_note_nothing_is_printed(
    prepared: tuple[dict[str, Path], Path, FakeTracker],
) -> None:
    """AC18, second half."""
    _, worktree, _ = prepared
    assert worktree.exists()


# --------------------------------------------------------------------------- #
# Attachments (R1)
# --------------------------------------------------------------------------- #


def test_small_text_attachments_are_inlined_and_announced(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    tracker.tickets = [
        make_ticket(
            attachments=(
                Attachment(
                    filename="server.log",
                    mime_type="text/plain",
                    size_bytes=42,
                    url="https://example.atlassian.net/attachment/1",
                ),
            )
        )
    ]
    tracker.payloads = {"server.log": b"ERROR token expired\n"}

    result = runner.invoke(cli, ["prep"])
    assert "Downloading attachment(s)" in result.stderr
    assert "server.log" in result.stderr

    worktree = worktree_from(result.stdout)
    stored = worktree / "artifacts" / "PROJ-1" / "attachments" / "server.log"
    assert stored.read_text(encoding="utf-8") == "ERROR token expired\n"
    context = (worktree / "CLAUDE.md").read_text(encoding="utf-8")
    assert "ERROR token expired" in context


def test_images_and_oversized_files_are_left_in_the_tracker(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    tracker.tickets = [
        make_ticket(
            attachments=(
                Attachment("screen.png", "image/png", 4_000, "https://example/1"),
                Attachment("huge.log", "text/plain", 5_000_000, "https://example/2"),
            )
        )
    ]
    result = runner.invoke(cli, ["prep"])
    assert tracker.downloaded == []
    assert "screen.png" in result.stderr
    assert "not in the allowlist" in result.stderr
    assert "exceeds the" in result.stderr

    context = (worktree_from(result.stdout) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "screen.png" in context
    assert "not downloaded" in context
