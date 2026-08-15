"""The console entry point, exercised as a process.

Every other test in this suite calls `CliRunner.invoke(cli, ...)`, which enters
at the group and never runs `main()`. That is the right tool for behaviour, and
it is also why K2 survived six audits: `main()` is where `standalone_mode=False`
is unwound, where exceptions become exit codes, and where a traceback would
actually reach a terminal. None of that had ever been executed by a test.

Deliberately few. The point is that this path exists and is checked at all, not
to reach a coverage number here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import write_config, write_credentials


def run_lazyfish(*args: str) -> subprocess.CompletedProcess:
    """Invoke lazyfish the way a shell does: a new process, through main().

    `-m lazyfish.cli` rather than the installed script so the test does not
    depend on an editable install being present, while still going through
    `if __name__ == "__main__": sys.exit(main())`.
    """
    return subprocess.run(
        [sys.executable, "-m", "lazyfish.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_version_exits_zero(env: dict[str, Path]) -> None:
    result = run_lazyfish("--version")
    assert result.returncode == 0
    assert "lazyfish" in result.stdout


def test_missing_config_exits_with_the_config_code_and_no_traceback(
    env: dict[str, Path],
) -> None:
    """AC13 through the entry point rather than around it."""
    result = run_lazyfish("status")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "lazyfish init" in result.stderr


def test_a_damaged_database_is_a_message_not_a_traceback(env: dict[str, Path], repo: Path) -> None:
    """K2: this is the exact shape that used to print a raw sqlite traceback."""
    write_config(env["config"], profiles={"work": {"repo": str(repo)}})
    write_credentials(env["credentials"])
    env["data"].mkdir(parents=True, exist_ok=True)
    (env["data"] / "lazyfish.db").write_text("not a database at all\n", encoding="utf-8")

    result = run_lazyfish("status")
    assert result.returncode == 7
    assert "Traceback" not in result.stderr
    # The likeliest cause is named, because the fix differs from the one for
    # genuine corruption.
    assert "LAZYFISH_DATA_DIR" in result.stderr


@pytest.mark.parametrize("args", [("--help",), ("prep", "--help")])
def test_help_exits_zero(env: dict[str, Path], args: tuple[str, ...]) -> None:
    """standalone_mode=False makes help a value main() has to interpret, not a raise."""
    result = run_lazyfish(*args)
    assert result.returncode == 0
    assert "Usage:" in result.stdout
