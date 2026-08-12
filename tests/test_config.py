"""Configuration validation, mostly the error paths.

Every assertion here checks the *message*, not just that something was raised.
This file is the first thing a new user meets, and an error that does not say
which key is wrong is only marginally better than a traceback (AC13, R6).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lazyfish.config import (
    DEFAULT_CONVENTIONS_PATH,
    DEFAULT_EMAIL_ENV,
    DEFAULT_TOKEN_ENV,
    load_config,
    parse_config,
)
from lazyfish.errors import ConfigError

from .conftest import write_config


def test_minimal_config_loads(env: dict[str, Path], repo: Path) -> None:
    write_config(env["config"], repos={"default": {"path": f'"{repo}"'}})
    config = load_config(env["config"])
    assert config.tracker.kind == "jira-cloud"
    assert config.tracker.base_url == "https://example.atlassian.net"
    assert config.repo(None).path == repo.resolve()
    assert config.repo(None).name == "default"


def test_missing_file_points_at_init(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "nope.toml")
    assert "lazyfish init" in str(excinfo.value)


def test_invalid_toml_is_reported_as_such(tmp_path: Path) -> None:
    broken = tmp_path / "config.toml"
    broken.write_text("[tracker\nkind = 'jira-cloud'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(broken)


def test_missing_tracker_key_names_the_key(env: dict[str, Path], repo: Path) -> None:
    env["config"].write_text(
        "[tracker]\n"
        'kind = "jira-cloud"\n'
        'base_url = "https://example.atlassian.net"\n'
        "\n[repo.default]\n"
        f'path = "{repo}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    message = str(excinfo.value)
    assert "query" in message
    assert "[tracker]" in message


# --------------------------------------------------------------------------- #
# Credential variable names (AC20, AC21)
# --------------------------------------------------------------------------- #


def test_env_names_default_when_the_keys_are_absent(env: dict[str, Path], repo: Path) -> None:
    """AC20: `init` writes neither key, so the defaults have to carry the load."""
    write_config(
        env["config"],
        repos={"default": {"path": f'"{repo}"'}},
        email_env=None,
        token_env=None,
    )
    written = env["config"].read_text(encoding="utf-8")
    assert "email_env" not in written
    assert "token_env" not in written

    tracker = load_config(env["config"]).tracker
    assert tracker.email_env == DEFAULT_EMAIL_ENV
    assert tracker.token_env == DEFAULT_TOKEN_ENV
    assert tracker.credentials() == ("tester@example.com", "not-a-real-token")


def test_env_names_can_be_overridden_per_config(
    env: dict[str, Path], repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC21: the keys stay readable for the two-instance case."""
    write_config(
        env["config"],
        repos={"default": {"path": f'"{repo}"'}},
        email_env="WORK_EMAIL",
        token_env="OTHER_VAR",
    )
    monkeypatch.setenv("WORK_EMAIL", "work@example.com")
    monkeypatch.setenv("OTHER_VAR", "work-token")

    tracker = load_config(env["config"]).tracker
    assert tracker.token_env == "OTHER_VAR"
    assert tracker.credentials() == ("work@example.com", "work-token")


def test_an_overridden_variable_that_is_unset_names_that_variable(
    env: dict[str, Path], repo: Path
) -> None:
    write_config(env["config"], repos={"default": {"path": f'"{repo}"'}}, token_env="OTHER_VAR")
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    message = str(excinfo.value)
    assert "OTHER_VAR" in message
    assert "LAZYFISH_TOKEN" not in message


def test_unknown_kind_lists_the_accepted_values(env: dict[str, Path], repo: Path) -> None:
    write_config(env["config"], repos={"default": {"path": f'"{repo}"'}})
    text = env["config"].read_text(encoding="utf-8").replace("jira-cloud", "linear")
    env["config"].write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    assert "jira-cloud" in str(excinfo.value)


def test_repo_path_that_does_not_exist(env: dict[str, Path], tmp_path: Path) -> None:
    write_config(env["config"], repos={"default": {"path": '"/does/not/exist"'}})
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    message = str(excinfo.value)
    assert "/does/not/exist" in message
    assert "[repo.default]" in message


def test_repo_path_without_git(env: dict[str, Path], tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    write_config(env["config"], repos={"default": {"path": f'"{plain}"'}})
    with pytest.raises(ConfigError, match="not a git repository"):
        load_config(env["config"])


def test_unknown_key_is_rejected_with_the_valid_list(env: dict[str, Path], repo: Path) -> None:
    write_config(
        env["config"],
        repos={"default": {"path": f'"{repo}"', "convention": '"typo.md"'}},
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    message = str(excinfo.value)
    assert "convention" in message
    assert "conventions" in message


def test_missing_environment_variable_names_it(
    env: dict[str, Path], repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(env["config"], repos={"default": {"path": f'"{repo}"'}})
    monkeypatch.delenv("LAZYFISH_TOKEN")
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    message = str(excinfo.value)
    assert "LAZYFISH_TOKEN" in message
    assert "export" in message


def test_credentials_are_not_required_for_offline_commands(
    env: dict[str, Path], repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(env["config"], repos={"default": {"path": f'"{repo}"'}})
    monkeypatch.delenv("LAZYFISH_TOKEN")
    config = load_config(env["config"], require_credentials=False)
    assert config.repo(None).name == "default"


def test_unknown_profile_lists_the_known_ones(env: dict[str, Path], repo: Path) -> None:
    write_config(env["config"], repos={"default": {"path": f'"{repo}"'}})
    config = load_config(env["config"])
    with pytest.raises(ConfigError) as excinfo:
        config.repo("frontend")
    message = str(excinfo.value)
    assert "frontend" in message
    assert "default" in message


def test_env_field_holding_a_value_instead_of_a_name(env: dict[str, Path], repo: Path) -> None:
    write_config(
        env["config"],
        repos={"default": {"path": f'"{repo}"'}},
        email_env="you@example.com",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    assert "NAME of an environment variable" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Credential detection (AC17)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "line",
    [
        'token = "ATATT3xFfGF0T4Nn2VXsY1qKpLmZ7bWc"',
        'token_env = "ATATT3xFfGF0T4Nn2VXsY1qKpLmZ7bWc"',
        'api_key = "sk-proj-Ab12Cd34Ef56Gh78Ij90"',
        'secret = "ghp_16C7e42F292c6912E7710c838347Ae178B4a"',
        'password = "Xk8!vQ2$mZ9pL4wR7tY1nB5c"',
    ],
)
def test_credential_literal_refuses_to_start(env: dict[str, Path], repo: Path, line: str) -> None:
    write_config(env["config"], repos={"default": {"path": f'"{repo}"'}})
    key = line.split(" = ")[0]
    kept = [
        existing
        for existing in env["config"].read_text(encoding="utf-8").splitlines()
        if not existing.startswith(f"{key} = ")
    ]
    text = "\n".join(kept).replace("[tracker]", f"[tracker]\n{line}", 1)
    env["config"].write_text(text + "\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(env["config"])
    message = str(excinfo.value)
    assert "credential" in message.lower()
    assert "environment variable" in message


def test_ordinary_long_values_are_not_flagged(env: dict[str, Path], repo: Path) -> None:
    """The heuristic must not fire on the values a real config always has."""
    write_config(
        env["config"],
        base_url="https://a-very-long-organisation-name.atlassian.net",
        query=(
            "assignee = currentUser() AND sprint in openSprints() "
            "ORDER BY priority DESC, created ASC"
        ),
        repos={
            "default": {
                "path": f'"{repo}"',
                "account_note": '"company seat - check the client sign-in"',
                "conventions": '"docs/engineering/conventions-for-this-repo.md"',
                "branch_prefix": '"lazyfish/"',
            }
        },
    )
    config = load_config(env["config"])
    assert config.repo(None).account_note == "company seat - check the client sign-in"


# --------------------------------------------------------------------------- #
# Conventions resolution
# --------------------------------------------------------------------------- #


def test_conventions_defaults_to_the_repo_local_path(env: dict[str, Path], repo: Path) -> None:
    write_config(env["config"], repos={"default": {"path": f'"{repo}"'}})
    profile = load_config(env["config"]).repo(None)
    assert profile.conventions == DEFAULT_CONVENTIONS_PATH
    assert profile.conventions_path() == repo.resolve() / DEFAULT_CONVENTIONS_PATH


def test_conventions_may_be_absolute(env: dict[str, Path], repo: Path, tmp_path: Path) -> None:
    """AC19: a file outside the repository is a supported configuration."""
    outside = tmp_path / "team" / "conventions.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("# rules\n", encoding="utf-8")
    write_config(
        env["config"],
        repos={"default": {"path": f'"{repo}"', "conventions": f'"{outside}"'}},
    )
    profile = load_config(env["config"]).repo(None)
    assert profile.conventions_path() == outside


def test_conventions_can_be_switched_off(env: dict[str, Path], repo: Path) -> None:
    write_config(
        env["config"],
        repos={"default": {"path": f'"{repo}"', "conventions": '""'}},
    )
    profile = load_config(env["config"]).repo(None)
    assert profile.conventions is None
    assert profile.conventions_path() is None


def test_search_globs_must_be_a_list_of_strings(env: dict[str, Path], repo: Path) -> None:
    write_config(
        env["config"],
        repos={"default": {"path": f'"{repo}"', "search_globs": '"*.py"'}},
    )
    with pytest.raises(ConfigError, match="list of strings"):
        load_config(env["config"])


def test_parse_config_rejects_a_document_without_repos(tmp_path: Path) -> None:
    data = {
        "tracker": {
            "kind": "jira-cloud",
            "base_url": "https://example.atlassian.net",
            "email_env": "LAZYFISH_EMAIL",
            "token_env": "LAZYFISH_TOKEN",
            "query": "assignee = currentUser()",
        }
    }
    with pytest.raises(ConfigError, match="No repo profiles"):
        parse_config(data, tmp_path / "config.toml")
