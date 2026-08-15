"""Configuration validation, mostly the error paths.

Every assertion here checks the *message*, not just that something was raised.
This file is the first thing a new user meets, and an error that does not say
which key is wrong is only marginally better than a traceback (AC13, R6).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lazyfish.config import (
    DEFAULT_ATTACHMENT_MAX_BYTES,
    DEFAULT_BRANCH_PREFIX,
    DEFAULT_CONVENTIONS_PATH,
    DEFAULT_TIMEOUT_SECONDS,
    load_config,
    parse_config,
    resolve_credentials,
)
from lazyfish.errors import ConfigError

from .conftest import write_config, write_credentials


def test_minimal_config_loads(env: dict[str, Path], repo: Path) -> None:
    write_config(env["config"], profiles={"work": {"repo": str(repo)}})
    profile = load_config(env["config"]).select()
    assert profile.name == "work"
    assert profile.tracker == "jira-cloud"
    assert profile.repo == repo.resolve()
    assert profile.query.startswith("project = WORK")


def test_missing_file_points_at_init(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "nope.toml")
    assert "lazyfish init" in str(excinfo.value)


def test_invalid_toml_is_reported_as_such(tmp_path: Path) -> None:
    broken = tmp_path / "config.toml"
    broken.write_text("[profile.work\nrepo = 'x'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(broken)


@pytest.mark.parametrize("key", ["tracker", "base_url", "query", "repo"])
def test_missing_identity_key_names_it(env: dict[str, Path], repo: Path, key: str) -> None:
    write_config(env["config"], profiles={"work": {"repo": str(repo)}})
    kept = [
        line
        for line in env["config"].read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{key} = ")
    ]
    env["config"].write_text("\n".join(kept) + "\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    message = str(excinfo.value)
    assert key in message
    assert "[profile.work]" in message


def test_unknown_tracker_lists_the_accepted_values(env: dict[str, Path], repo: Path) -> None:
    write_config(env["config"], profiles={"work": {"repo": str(repo), "tracker": "linear"}})
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    assert "jira-cloud" in str(excinfo.value)


def test_repo_that_does_not_exist(env: dict[str, Path]) -> None:
    """Loading succeeds; the check happens when a command needs the checkout.

    A profile whose repository is on an unmounted disk must not stop the
    commands that never touch it, such as `list` (LF-3 AC9).
    """
    write_config(env["config"], profiles={"work": {"repo": "/does/not/exist"}})
    profile = load_config(env["config"]).select()

    with pytest.raises(ConfigError) as excinfo:
        profile.require_repo()
    message = str(excinfo.value)
    assert "/does/not/exist" in message
    assert "[profile.work]" in message


def test_repo_without_git(env: dict[str, Path], tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    write_config(env["config"], profiles={"work": {"repo": str(plain)}})
    with pytest.raises(ConfigError, match="not a git repository"):
        load_config(env["config"]).select().require_repo()


def test_unknown_key_is_rejected_with_the_valid_list(env: dict[str, Path], repo: Path) -> None:
    write_config(env["config"], profiles={"work": {"repo": str(repo), "convention": "typo.md"}})
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    message = str(excinfo.value)
    assert "convention" in message
    assert "conventions" in message


def test_no_profiles_at_all(env: dict[str, Path]) -> None:
    env["config"].write_text('default_profile = "work"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="No profiles defined"):
        load_config(env["config"])


def test_default_profile_must_name_a_real_profile(env: dict[str, Path], repo: Path) -> None:
    write_config(env["config"], profiles={"work": {"repo": str(repo)}}, default_profile="typo")
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    message = str(excinfo.value)
    assert "typo" in message
    assert "work" in message


def test_parse_config_rejects_a_non_table_profile(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must be a table"):
        parse_config({"profile": {"work": "oops"}}, tmp_path / "config.toml")


# --------------------------------------------------------------------------- #
# Identity keys may not be defaulted (AC12)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["tracker", "base_url", "query", "repo"])
def test_identity_key_in_defaults_is_refused(env: dict[str, Path], repo: Path, key: str) -> None:
    """AC12: sharing a query across profiles is the defect this design removes."""
    write_config(
        env["config"],
        profiles={"work": {"repo": str(repo)}},
        defaults={key: "anything"},
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    message = str(excinfo.value)
    assert key in message
    assert "[defaults]" in message
    assert "profile" in message


def test_unknown_key_in_defaults_is_refused(env: dict[str, Path], repo: Path) -> None:
    write_config(env["config"], profiles={"work": {"repo": str(repo)}}, defaults={"nonsense": 1})
    with pytest.raises(ConfigError, match="nonsense"):
        load_config(env["config"])


# --------------------------------------------------------------------------- #
# Built-in fallbacks (AC11)
# --------------------------------------------------------------------------- #


def test_without_a_defaults_table_everything_falls_back(env: dict[str, Path], repo: Path) -> None:
    """AC11: the example values in the docs are the built-ins, not new defaults."""
    write_config(env["config"], profiles={"work": {"repo": str(repo)}})
    profile = load_config(env["config"]).select()
    assert profile.conventions == DEFAULT_CONVENTIONS_PATH
    assert profile.search_globs == ()
    assert profile.account_note is None
    assert profile.base_branch is None
    assert profile.branch_prefix == DEFAULT_BRANCH_PREFIX
    assert profile.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert profile.attachment_max_bytes == DEFAULT_ATTACHMENT_MAX_BYTES
    assert profile.attachment_mime_allowlist[0] == "text/plain"
    assert profile.worktree_root_path().name == "worktrees"


# --------------------------------------------------------------------------- #
# Credential literal detection (AC9)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "line",
    [
        'api_token = "ATATT3xFfGF0T4Nn2VXsY1qKpLmZ7bWc"',
        'token = "sk-proj-Ab12Cd34Ef56Gh78Ij90"',
        'secret = "ghp_16C7e42F292c6912E7710c838347Ae178B4a"',
        'password = "Xk8!vQ2$mZ9pL4wR7tY1nB5c"',
    ],
)
def test_credential_in_config_toml_refuses_to_start(
    env: dict[str, Path], repo: Path, line: str
) -> None:
    """AC9, first half."""
    write_config(env["config"], profiles={"work": {"repo": str(repo)}})
    text = env["config"].read_text(encoding="utf-8")
    env["config"].write_text(
        text.replace("[profile.work]", f"[profile.work]\n{line}"), encoding="utf-8"
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    message = str(excinfo.value)
    assert "credential" in message.lower()
    assert "credentials" in message
    assert "api_token" in message


def test_the_same_token_in_the_credentials_file_is_fine(env: dict[str, Path], repo: Path) -> None:
    """AC9, second half: no warning, no error - that file is where it belongs."""
    write_config(env["config"], profiles={"work": {"repo": str(repo)}})
    write_credentials(
        env["credentials"],
        sections={
            "work": {
                "email": "you@example.com",
                "api_token": "ATATT3xFfGF0T4Nn2VXsY1qKpLmZ7bWc",
            }
        },
    )
    load_config(env["config"])
    credentials = resolve_credentials("work", env["credentials"])
    assert credentials.api_token == "ATATT3xFfGF0T4Nn2VXsY1qKpLmZ7bWc"


def test_ordinary_long_values_are_not_flagged(env: dict[str, Path], repo: Path) -> None:
    write_config(
        env["config"],
        profiles={
            "work": {
                "repo": str(repo),
                "base_url": "https://a-very-long-organisation-name.atlassian.net",
                "query": (
                    "assignee = currentUser() AND sprint in openSprints() "
                    "ORDER BY priority DESC, created ASC"
                ),
                "account_note": "company seat - check the client sign-in",
                "conventions": "docs/engineering/conventions-for-this-repo.md",
            }
        },
    )
    profile = load_config(env["config"]).select()
    assert profile.account_note == "company seat - check the client sign-in"


# --------------------------------------------------------------------------- #
# Credentials file
# --------------------------------------------------------------------------- #


def test_missing_credentials_section_names_profile_and_path(env: dict[str, Path]) -> None:
    """AC14."""
    write_credentials(env["credentials"], sections={"other": {"email": "a@b.c", "api_token": "t"}})
    with pytest.raises(ConfigError) as excinfo:
        resolve_credentials("work", env["credentials"])
    message = str(excinfo.value)
    assert "work" in message
    assert str(env["credentials"]) in message


def test_missing_credentials_file_is_explained(env: dict[str, Path]) -> None:
    with pytest.raises(ConfigError) as excinfo:
        resolve_credentials("work", env["credentials"])
    message = str(excinfo.value)
    assert str(env["credentials"]) in message
    assert "lazyfish init" in message


@pytest.mark.parametrize("key", ["email", "api_token"])
def test_missing_credential_field_names_it(env: dict[str, Path], key: str) -> None:
    section = {"email": "you@example.com", "api_token": "token"}
    del section[key]
    write_credentials(env["credentials"], sections={"work": section})
    with pytest.raises(ConfigError) as excinfo:
        resolve_credentials("work", env["credentials"])
    assert key in str(excinfo.value)


@pytest.mark.skipif(os.name != "posix", reason="mode bits only mean something on POSIX")
def test_world_readable_credentials_are_refused(env: dict[str, Path]) -> None:
    """AC8: the message has to be copy-pasteable."""
    write_credentials(env["credentials"], mode=0o644)
    with pytest.raises(ConfigError) as excinfo:
        resolve_credentials("work", env["credentials"])
    message = str(excinfo.value)
    assert "chmod 600" in message
    assert str(env["credentials"]) in message


@pytest.mark.skipif(os.name != "posix", reason="mode bits only mean something on POSIX")
def test_mode_600_is_accepted(env: dict[str, Path]) -> None:
    write_credentials(env["credentials"], mode=0o600)
    assert resolve_credentials("work", env["credentials"]).api_token == "token-work"


# --------------------------------------------------------------------------- #
# --config and $LAZYFISH_CONFIG name the same thing (K1)
# --------------------------------------------------------------------------- #
#
# These go through the CLI on purpose. The defect they cover could not be seen
# from `resolve_credentials` alone: both halves were correct in isolation, and
# only the wiring disagreed about which directory the file was in. Every other
# test in this file relocates config with $LAZYFISH_CONFIG, which is the path
# that always worked.


def test_config_option_puts_credentials_where_the_next_command_reads_them(
    env: dict[str, Path],
    repo: Path,
    tracker: object,
    runner: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K1: init --config wrote the token to a directory nothing else looked in."""
    from lazyfish.cli import cli

    monkeypatch.delenv("LAZYFISH_CONFIG", raising=False)
    alternate = tmp_path / "alternate" / "config.toml"

    created = runner.invoke(
        cli,
        [
            "--config",
            str(alternate),
            "init",
            "--yes",
            "--profile",
            "work",
            "--base-url",
            "https://example.atlassian.net",
            "--email",
            "you@example.com",
            "--api-token",
            "token-work",
            "--query",
            "project = PROJ",
            "--repository",
            str(repo),
            "--no-check",
        ],
    )
    assert created.exit_code == 0, created.output

    # Written beside the config file it was told to use, and nowhere else.
    assert (alternate.parent / "credentials").exists()
    assert not env["credentials"].exists()

    # The round trip: the same --config finds what init just wrote.
    listed = runner.invoke(cli, ["--config", str(alternate), "list"])
    assert listed.exit_code == 0, listed.output


def test_config_env_var_still_locates_credentials_beside_it(
    env: dict[str, Path],
    repo: Path,
    tracker: object,
    runner: object,
) -> None:
    """The half that was already right stays right."""
    from lazyfish.cli import cli

    created = runner.invoke(
        cli,
        [
            "init",
            "--yes",
            "--profile",
            "work",
            "--base-url",
            "https://example.atlassian.net",
            "--email",
            "you@example.com",
            "--api-token",
            "token-work",
            "--query",
            "project = PROJ",
            "--repository",
            str(repo),
            "--no-check",
        ],
    )
    assert created.exit_code == 0, created.output
    assert env["credentials"].exists()

    listed = runner.invoke(cli, ["list"])
    assert listed.exit_code == 0, listed.output
