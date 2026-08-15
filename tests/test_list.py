"""The read-only `list` command.

Two things carry the weight here. First, that the command really is read-only:
its entire value over "prep, look, abandon" is that it leaves nothing behind, so
AC1 compares the disk and the database before and after. Second, that the table
lines up when titles are not ASCII - this project passes ticket text through
verbatim, so a Chinese title is the normal case, not an exotic one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lazyfish.cli import cli
from lazyfish.db import Database
from lazyfish.rendering import Column, display_width, render_table, truncate_to_width

from .conftest import FakeTracker, make_ticket, write_config, write_credentials


def snapshot(env: dict[str, Path]) -> dict[str, object]:
    """Everything `list` is forbidden to change."""
    worktrees = env["data"] / "worktrees"
    database = env["data"] / "lazyfish.db"
    rows: list[tuple] = []
    if database.exists():
        instance = Database(database)
        try:
            rows = [
                (task.id, task.ticket_key, task.profile, task.state)
                for task in instance.list_tasks()
            ]
        finally:
            instance.close()
    return {
        "worktree_tree": sorted(str(p.relative_to(worktrees)) for p in worktrees.rglob("*"))
        if worktrees.exists()
        else None,
        "database_exists": database.exists(),
        "rows": rows,
    }


# --------------------------------------------------------------------------- #
# AC1, AC2: no side effects, and no WIP gate
# --------------------------------------------------------------------------- #


def test_list_changes_nothing_on_a_clean_machine(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC1, first state: nothing recorded locally."""
    tracker.tickets = [make_ticket("PROJ-1"), make_ticket("PROJ-2")]
    before = snapshot(configured)

    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "PROJ-1" in result.stdout

    assert snapshot(configured) == before
    # Specifically: looking at the queue must not bring a database into being.
    assert before["database_exists"] is False


def test_list_changes_nothing_with_a_ticket_in_flight(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC1, second state, and AC2: a busy profile is when you most need the queue."""
    tracker.tickets = [make_ticket("PROJ-1"), make_ticket("PROJ-2")]
    assert runner.invoke(cli, ["prep"]).exit_code == 0
    before = snapshot(configured)
    assert before["rows"], "the fixture should have left one task in flight"

    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "PROJ-2" in result.stdout

    assert snapshot(configured) == before


# --------------------------------------------------------------------------- #
# AC3: local state annotation
# --------------------------------------------------------------------------- #


def test_tickets_already_known_locally_are_marked(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC3. This annotation is the reason to use it over the Jira web UI."""
    tracker.tickets = [make_ticket("PROJ-1"), make_ticket("PROJ-2")]
    runner.invoke(cli, ["prep"])  # PROJ-1 becomes locally known

    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    lines = {
        line.split()[1] if line.strip().startswith("*") else line.split()[0]: line
        for line in result.stdout.splitlines()
        if "PROJ-" in line
    }
    assert lines["PROJ-1"].strip().startswith("*")
    assert not lines["PROJ-2"].strip().startswith("*")
    assert "awaiting artifact" in result.stdout.lower()


def test_a_clean_queue_has_no_markers(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    tracker.tickets = [make_ticket("PROJ-1")]
    result = runner.invoke(cli, ["list"])
    assert "already tracked locally" not in result.stdout


# --------------------------------------------------------------------------- #
# AC4: display width
# --------------------------------------------------------------------------- #


def test_chinese_titles_stay_aligned_and_verbatim(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC4: full-width characters take two columns, and the text is untouched."""
    chinese = "登录后密码重置链接立即失效"
    tracker.tickets = [
        make_ticket("PROJ-1", title=chinese),
        make_ticket("PROJ-2", title="ASCII title for comparison"),
    ]
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert chinese in result.stdout

    rows = [line for line in result.stdout.splitlines() if "PROJ-" in line]
    assert len(rows) == 2
    # The summary column starts at the same display offset on both rows.
    offsets = [
        display_width(line[: line.index(title)])
        for line, title in zip(rows, [chinese, "ASCII"], strict=True)
    ]
    assert offsets[0] == offsets[1]


def test_display_width_counts_columns_not_characters() -> None:
    assert display_width("abc") == 3
    assert display_width("登录") == 4
    assert display_width("a登b") == 4
    assert display_width("") == 0
    # Combining marks render on top of the previous character.
    assert display_width("café") == 4


def test_display_width_does_not_crash_on_emoji() -> None:
    """Terminals disagree about emoji width; the renderer must not fall over."""
    assert display_width("done \U0001f600") >= 5


def test_truncate_respects_display_width() -> None:
    assert truncate_to_width("abcdefghij", 5) == "ab..."
    assert truncate_to_width("abc", 10) == "abc"
    assert truncate_to_width("登录后密码重置", 8) == "登录..."
    assert display_width(truncate_to_width("登录后密码重置", 8)) <= 8
    assert truncate_to_width("anything", 2) == ".."
    assert truncate_to_width("anything", 0) == ""


def test_render_table_pads_by_width() -> None:
    lines = render_table(
        [Column("KEY"), Column("SUMMARY")],
        [["A-1", "登录失败"], ["A-22", "short"]],
    )
    assert lines[0].startswith("  KEY")
    # Every row's second column begins at the same display offset.
    starts = [
        display_width(line[: line.index(cell)])
        for line, cell in zip(lines[1:], ["登录失败", "short"], strict=True)
    ]
    assert starts[0] == starts[1]
    # No trailing whitespace anywhere.
    assert all(line == line.rstrip() for line in lines)


def test_render_table_is_empty_without_rows() -> None:
    assert render_table([Column("KEY")], []) == []


# --------------------------------------------------------------------------- #
# AC5: limits
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
def test_limits_outside_the_range_are_refused(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker, limit: int
) -> None:
    """AC5: refused, not silently clamped."""
    result = runner.invoke(cli, ["list", "--limit", str(limit)])
    assert result.exit_code != 0
    assert "between 1 and 100" in result.stderr
    assert "Traceback" not in result.stderr


def test_limit_is_passed_through(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    tracker.tickets = [make_ticket(f"PROJ-{index}") for index in range(1, 11)]
    result = runner.invoke(cli, ["list", "--limit", "3"])
    assert result.exit_code == 0
    assert len([line for line in result.stdout.splitlines() if "PROJ-" in line]) == 3
    assert "3 tickets" in result.stdout


# --------------------------------------------------------------------------- #
# AC6: machine-readable output
# --------------------------------------------------------------------------- #


def test_json_output_has_the_documented_shape(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC6."""
    tracker.tickets = [make_ticket("PROJ-1"), make_ticket("PROJ-2")]
    runner.invoke(cli, ["prep"])

    result = runner.invoke(cli, ["list", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr

    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert [item["key"] for item in payload] == ["PROJ-1", "PROJ-2"]
    for item in payload:
        assert set(item) == {
            "key",
            "summary",
            "priority",
            "status",
            "is_known",
            "known_state",
        }
    assert payload[0]["is_known"] is True
    assert payload[0]["known_state"] == "AWAITING_ARTIFACT"
    assert payload[1]["is_known"] is False
    assert payload[1]["known_state"] is None

    # Nothing but JSON: no header, no table, no hint line.
    assert "PROFILE" not in result.stdout
    assert "prep --ticket" not in result.stdout


def test_json_keeps_non_ascii_readable(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    tracker.tickets = [make_ticket("PROJ-1", title="登录失败")]
    result = runner.invoke(cli, ["list", "--json"])
    assert "登录失败" in result.stdout
    assert json.loads(result.stdout)[0]["summary"] == "登录失败"


# --------------------------------------------------------------------------- #
# AC7, AC10, AC11: empty results, profile selection, the hint
# --------------------------------------------------------------------------- #


def test_an_empty_queue_exits_zero_with_an_explanation(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC7."""
    tracker.tickets = []
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "No tickets matched" in result.stdout
    assert "KEY" not in result.stdout


def test_header_shows_the_profile_and_its_note(
    runner: CliRunner, env: dict[str, Path], repo: Path, tracker: FakeTracker
) -> None:
    """AC10."""
    write_config(
        env["config"],
        profiles={"work": {"repo": str(repo), "account_note": "company seat"}},
    )
    write_credentials(env["credentials"])
    result = runner.invoke(cli, ["list"])
    assert "PROFILE" in result.stdout
    assert "work" in result.stdout
    assert "company seat" in result.stdout
    assert "QUERY" in result.stdout


def test_profile_selection_follows_the_usual_chain(
    runner: CliRunner, two_profiles: dict[str, Path], tracker: FakeTracker, monkeypatch
) -> None:
    """AC10, the rest: same precedence as every other command."""
    tracker.by_query = {
        "project = WORK": [make_ticket("WORK-1")],
        "project = INFRA": [make_ticket("INFRA-9")],
    }
    assert "WORK-1" in runner.invoke(cli, ["list"]).stdout
    assert "INFRA-9" in runner.invoke(cli, ["--profile", "infra", "list"]).stdout

    monkeypatch.setenv("LAZYFISH_PROFILE", "infra")
    assert "INFRA-9" in runner.invoke(cli, ["list"]).stdout


def test_output_ends_with_the_way_to_act_on_it(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC11."""
    tracker.tickets = [make_ticket("PROJ-1")]
    result = runner.invoke(cli, ["list"])
    assert "lazyfish prep --ticket <KEY>" in result.stdout


# --------------------------------------------------------------------------- #
# AC9: a repository that is not on disk
# --------------------------------------------------------------------------- #


def test_list_works_when_the_repo_is_missing_but_prep_does_not(
    runner: CliRunner, env: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC9: list never touches the checkout, so it must not demand one."""
    write_config(env["config"], profiles={"work": {"repo": "/does/not/exist"}})
    write_credentials(env["credentials"])
    tracker.tickets = [make_ticket("PROJ-1")]

    listed = runner.invoke(cli, ["list"])
    assert listed.exit_code == 0, listed.stdout + listed.stderr
    assert "PROJ-1" in listed.stdout

    prepped = runner.invoke(cli, ["prep"])
    assert prepped.exit_code != 0
    assert "/does/not/exist" in prepped.stderr
    assert "Traceback" not in prepped.stderr


# --------------------------------------------------------------------------- #
# Failure paths stay readable
# --------------------------------------------------------------------------- #


def test_missing_credentials_are_reported_the_same_way(
    runner: CliRunner, env: dict[str, Path], repo: Path, tracker: FakeTracker
) -> None:
    write_config(env["config"], profiles={"work": {"repo": str(repo)}})
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 2
    assert str(env["credentials"]) in result.stderr
    assert "Traceback" not in result.stderr


def test_an_unknown_profile_is_reported_the_same_way(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    result = runner.invoke(cli, ["--profile", "nope", "list"])
    assert result.exit_code != 0
    assert "nope" in result.stderr
    assert "work" in result.stderr
