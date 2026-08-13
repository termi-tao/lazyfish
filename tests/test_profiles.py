"""Multi-profile semantics: the point of the restructure.

The property under test is that a query and a repository always travel together.
A mechanical rename - the old per-repository tables and selection flag given
new names - would leave the original defect intact while looking finished, so
AC1 is asserted on the value that actually reached the tracker, not on the name
of anything.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from lazyfish.cli import cli
from lazyfish.config import load_config, resolve_credentials
from lazyfish.db import Database
from lazyfish.errors import ConfigError

from .conftest import FakeTracker, make_ticket, write_config, write_credentials


def worktree_from(stdout: str) -> Path:
    last = stdout.strip().splitlines()[-1]
    assert last.startswith("cd "), last
    return Path(last[3:])


def tasks_of(env: dict[str, Path]) -> list:
    database = Database(env["data"] / "lazyfish.db")
    database.initialise()
    try:
        return database.list_tasks()
    finally:
        database.close()


# --------------------------------------------------------------------------- #
# AC1: the critical one
# --------------------------------------------------------------------------- #


def test_each_profile_uses_its_own_query_and_its_own_repo(
    runner: CliRunner,
    two_profiles: dict[str, Path],
    repo: Path,
    second_repo: Path,
    tracker: FakeTracker,
) -> None:
    """AC1. If this fails the refactor failed, whatever else passes."""
    tracker.by_query = {
        "project = WORK": [make_ticket("WORK-1", title="Reset links expire")],
        "project = INFRA": [make_ticket("INFRA-9", title="Rotate the runner tokens")],
    }

    work = runner.invoke(cli, ["--profile", "work", "prep"])
    assert work.exit_code == 0, work.stdout + work.stderr
    infra = runner.invoke(cli, ["--profile", "infra", "prep"])
    assert infra.exit_code == 0, infra.stdout + infra.stderr

    # Different tickets, because the queries differed.
    assert "WORK-1" in work.stdout
    assert "INFRA-9" in infra.stdout

    # And each worktree sits under its own profile's repository.
    work_tree = worktree_from(work.stdout)
    infra_tree = worktree_from(infra.stdout)
    assert (work_tree / "src" / "auth" / "reset_token.py").exists()
    assert (infra_tree / "index.ts").exists()

    rows = {task.profile: task.ticket_key for task in tasks_of(two_profiles)}
    assert rows == {"work": "WORK-1", "infra": "INFRA-9"}


def test_the_query_reaching_the_tracker_comes_from_the_selected_profile(
    runner: CliRunner, two_profiles: dict[str, Path], tracker: FakeTracker
) -> None:
    """The join key holds all the way down to the client constructor."""
    tracker.by_query = {
        "project = WORK": [make_ticket("WORK-1")],
        "project = INFRA": [make_ticket("INFRA-9")],
    }
    runner.invoke(cli, ["--profile", "infra", "prep"])
    name, query, token = tracker.built[-1]
    assert name == "infra"
    assert query == "project = INFRA"
    assert token == "token-infra"


def test_profiles_do_not_share_state(
    runner: CliRunner, two_profiles: dict[str, Path], tracker: FakeTracker
) -> None:
    """One ticket in flight per profile, not one globally (D3)."""
    tracker.by_query = {
        "project = WORK": [make_ticket("WORK-1"), make_ticket("WORK-2")],
        "project = INFRA": [make_ticket("INFRA-9")],
    }
    assert runner.invoke(cli, ["--profile", "work", "prep"]).exit_code == 0
    assert runner.invoke(cli, ["--profile", "infra", "prep"]).exit_code == 0

    # A second prep on a busy profile changes nothing.
    again = runner.invoke(cli, ["--profile", "work", "prep"])
    assert again.exit_code == 0
    assert "nothing to do" in again.stderr
    assert len(tasks_of(two_profiles)) == 2


# --------------------------------------------------------------------------- #
# AC13: which profile gets selected
# --------------------------------------------------------------------------- #


def test_command_line_beats_environment_beats_default(
    runner: CliRunner,
    two_profiles: dict[str, Path],
    tracker: FakeTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC13, all three combinations."""
    config = load_config(two_profiles["config"])

    # default_profile only
    assert config.select(None).name == "work"

    # LAZYFISH_PROFILE overrides default_profile
    monkeypatch.setenv("LAZYFISH_PROFILE", "infra")
    assert config.select(None).name == "infra"

    # --profile overrides LAZYFISH_PROFILE
    assert config.select("work").name == "work"


def test_environment_selection_works_end_to_end(
    runner: CliRunner,
    two_profiles: dict[str, Path],
    tracker: FakeTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker.by_query = {"project = INFRA": [make_ticket("INFRA-9")]}
    monkeypatch.setenv("LAZYFISH_PROFILE", "infra")
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "INFRA-9" in result.stdout
    assert tasks_of(two_profiles)[0].profile == "infra"


def test_unknown_profile_lists_the_available_ones(
    runner: CliRunner, two_profiles: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC15."""
    result = runner.invoke(cli, ["--profile", "nope", "prep"])
    assert result.exit_code != 0
    message = result.stderr
    assert "nope" in message
    assert "work" in message
    assert "infra" in message
    assert "Traceback" not in message


def test_a_single_profile_needs_no_default_profile(env: dict[str, Path], repo: Path) -> None:
    write_config(env["config"], profiles={"solo": {"repo": str(repo)}}, default_profile=None)
    assert load_config(env["config"]).select().name == "solo"


def test_several_profiles_without_a_default_ask_for_one(
    env: dict[str, Path], repo: Path, second_repo: Path
) -> None:
    write_config(
        env["config"],
        profiles={"work": {"repo": str(repo)}, "infra": {"repo": str(second_repo)}},
        default_profile=None,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"]).select()
    message = str(excinfo.value)
    assert "default_profile" in message
    assert "--profile" in message


# --------------------------------------------------------------------------- #
# AC4: two-level override, one test per key
# --------------------------------------------------------------------------- #


OVERRIDE_CASES = [
    ("timeout_seconds", 30, 90, 30.0, 90.0),
    ("attachment_max_bytes", 1000, 2000, 1000, 2000),
    (
        "attachment_mime_allowlist",
        ["text/plain"],
        ["text/csv"],
        ("text/plain",),
        ("text/csv",),
    ),
    ("branch_prefix", "shared/", "own/", "shared/", "own/"),
    ("base_branch", "main", "develop", "main", "develop"),
    ("conventions", "shared.md", "own.md", "shared.md", "own.md"),
    ("search_globs", ["*.py"], ["*.ts"], ("*.py",), ("*.ts",)),
    ("account_note", "shared seat", "own seat", "shared seat", "own seat"),
]


@pytest.mark.parametrize(
    "key,default_value,override_value,expected_default,expected_override",
    OVERRIDE_CASES,
    ids=[case[0] for case in OVERRIDE_CASES],
)
def test_profile_overrides_defaults(
    env: dict[str, Path],
    repo: Path,
    second_repo: Path,
    key: str,
    default_value: object,
    override_value: object,
    expected_default: object,
    expected_override: object,
) -> None:
    """AC4: the overriding profile gets its own value, the other gets the default."""
    write_config(
        env["config"],
        defaults={key: default_value},
        profiles={
            "work": {"repo": str(repo), key: override_value},
            "infra": {"repo": str(second_repo)},
        },
    )
    config = load_config(env["config"])
    assert getattr(config.select("work"), key) == expected_override
    assert getattr(config.select("infra"), key) == expected_default


def test_worktree_root_override(env: dict[str, Path], repo: Path, tmp_path: Path) -> None:
    """The ninth key, checked separately because it resolves to a Path."""
    shared = tmp_path / "shared-trees"
    own = tmp_path / "own-trees"
    write_config(
        env["config"],
        defaults={"worktree_root": str(shared)},
        profiles={
            "work": {"repo": str(repo), "worktree_root": str(own)},
            "infra": {"repo": str(repo)},
        },
    )
    config = load_config(env["config"])
    assert config.select("work").worktree_root_path() == own
    assert config.select("infra").worktree_root_path() == shared


# --------------------------------------------------------------------------- #
# AC5: falsy overrides are values, not absences
# --------------------------------------------------------------------------- #


def test_empty_conventions_switches_the_section_off(env: dict[str, Path], repo: Path) -> None:
    """AC5: "" must not fall back to [defaults] or to the built-in path (R2)."""
    write_config(
        env["config"],
        defaults={"conventions": "team/conventions.md"},
        profiles={"work": {"repo": str(repo), "conventions": ""}},
    )
    profile = load_config(env["config"]).select("work")
    assert profile.conventions is None
    assert profile.conventions_path() is None


def test_empty_search_globs_means_no_filter_not_inheritance(
    env: dict[str, Path], repo: Path
) -> None:
    """AC5 and D8: [] means "search everything", and it does not inherit."""
    write_config(
        env["config"],
        defaults={"search_globs": ["*.md"]},
        profiles={"work": {"repo": str(repo), "search_globs": []}},
    )
    assert load_config(env["config"]).select("work").search_globs == ()


def test_zero_is_a_value_too(env: dict[str, Path], repo: Path) -> None:
    write_config(
        env["config"],
        defaults={"attachment_max_bytes": 5000},
        profiles={"work": {"repo": str(repo), "attachment_max_bytes": 0}},
    )
    assert load_config(env["config"]).select("work").attachment_max_bytes == 0


# --------------------------------------------------------------------------- #
# AC6, AC6b, AC17: credential resolution
# --------------------------------------------------------------------------- #


def test_environment_overrides_the_credentials_file(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6, first half."""
    write_credentials(env["credentials"])
    monkeypatch.setenv("LAZYFISH_EMAIL", "env@example.com")
    monkeypatch.setenv("LAZYFISH_TOKEN", "token-from-environment")
    credentials = resolve_credentials("work", env["credentials"])
    assert credentials.email == "env@example.com"
    assert credentials.api_token == "token-from-environment"


@pytest.mark.skipif(os.name != "posix", reason="mode bits only mean something on POSIX")
def test_both_variables_set_skips_the_permission_check(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6, second half: with nothing to read, there is nothing to protect (D6)."""
    write_credentials(env["credentials"], mode=0o644)
    monkeypatch.setenv("LAZYFISH_EMAIL", "env@example.com")
    monkeypatch.setenv("LAZYFISH_TOKEN", "token-from-environment")
    assert resolve_credentials("work", env["credentials"]).api_token == "token-from-environment"


@pytest.mark.skipif(os.name != "posix", reason="mode bits only mean something on POSIX")
def test_half_the_credentials_in_the_environment_still_checks_the_file(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6b: the email still has to be read, so the file is still protected."""
    write_credentials(env["credentials"], mode=0o644)
    monkeypatch.setenv("LAZYFISH_TOKEN", "token-from-environment")
    with pytest.raises(ConfigError, match="chmod 600"):
        resolve_credentials("work", env["credentials"])


def test_no_credentials_file_is_fine_when_both_variables_are_set(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17."""
    assert not env["credentials"].exists()
    monkeypatch.setenv("LAZYFISH_EMAIL", "env@example.com")
    monkeypatch.setenv("LAZYFISH_TOKEN", "token-from-environment")
    assert resolve_credentials("work", env["credentials"]).email == "env@example.com"


def test_prep_runs_with_credentials_from_the_environment_only(
    runner: CliRunner,
    env: dict[str, Path],
    repo: Path,
    tracker: FakeTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC17 end to end: no credentials file anywhere, and prep still works."""
    write_config(env["config"], profiles={"work": {"repo": str(repo)}})
    monkeypatch.setenv("LAZYFISH_EMAIL", "env@example.com")
    monkeypatch.setenv("LAZYFISH_TOKEN", "token-from-environment")
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert tracker.built[-1][2] == "token-from-environment"


def test_a_profile_without_credentials_fails_with_both_names(
    runner: CliRunner, env: dict[str, Path], repo: Path, second_repo: Path, tracker: FakeTracker
) -> None:
    """AC14 through the CLI."""
    write_config(
        env["config"],
        profiles={"work": {"repo": str(repo)}, "infra": {"repo": str(second_repo)}},
    )
    write_credentials(env["credentials"], sections={"work": {"email": "a@b.c", "api_token": "t"}})

    assert runner.invoke(cli, ["--profile", "work", "prep"]).exit_code == 0
    result = runner.invoke(cli, ["--profile", "infra", "prep"])
    assert result.exit_code != 0
    assert "infra" in result.stderr
    assert str(env["credentials"]) in result.stderr
    assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------- #
# AC7: nothing is read from the current directory or the repository
# --------------------------------------------------------------------------- #


@pytest.fixture
def xdg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The real config location, reached without any explicit override.

    The rest of the suite isolates itself with LAZYFISH_CONFIG, which is an
    explicit override and therefore cannot prove anything about searching. Here
    the standard XDG variable is used instead, so the path under test is the one
    a user actually gets.
    """
    for name in ("LAZYFISH_CONFIG", "LAZYFISH_DATA_DIR", "LAZYFISH_EMAIL", "LAZYFISH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    config_home = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    return config_home / "lazyfish"


def plant_decoys(directory: Path, repo: Path) -> None:
    """A convincing config.toml and credentials in both places a search would find."""
    for location in (directory, repo):
        (location / "config.toml").write_text(
            'default_profile = "decoy"\n\n'
            "[profile.decoy]\n"
            'tracker  = "jira-cloud"\n'
            'base_url = "https://decoy.atlassian.net"\n'
            'query    = "project = DECOY"\n'
            f'repo     = "{repo}"\n',
            encoding="utf-8",
        )
        (location / "credentials").write_text(
            '[decoy]\nemail = "decoy@example.com"\napi_token = "ATATTdecoytoken"\n',
            encoding="utf-8",
        )


def test_config_files_in_the_working_directory_and_repo_are_ignored(
    runner: CliRunner,
    xdg_home: Path,
    repo: Path,
    tmp_path: Path,
    tracker: FakeTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7. Also the reason the tool does not search: worktrees are fresh checkouts."""
    write_config(xdg_home / "config.toml", profiles={"work": {"repo": str(repo)}})
    write_credentials(xdg_home / "credentials")

    workdir = tmp_path / "somewhere"
    workdir.mkdir()
    plant_decoys(workdir, repo)
    monkeypatch.chdir(workdir)

    tracker.by_query = {
        "project = WORK AND assignee = currentUser()": [make_ticket("WORK-1")],
        "project = DECOY": [make_ticket("DECOY-666")],
    }
    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr

    # The real profile ran; neither decoy contributed anything.
    assert "WORK-1" in result.stdout
    assert "DECOY" not in result.stdout + result.stderr
    name, query, token = tracker.built[-1]
    assert (name, query, token) == (
        "work",
        "project = WORK AND assignee = currentUser()",
        "token-work",
    )


def test_a_decoy_alone_is_not_enough_to_run(
    runner: CliRunner,
    xdg_home: Path,
    repo: Path,
    tmp_path: Path,
    tracker: FakeTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7, the other direction: with no real config, the decoys do not stand in."""
    workdir = tmp_path / "somewhere"
    workdir.mkdir()
    plant_decoys(workdir, repo)
    monkeypatch.chdir(workdir)

    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 2
    assert str(xdg_home / "config.toml") in result.stderr
    assert "lazyfish init" in result.stderr
