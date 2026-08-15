"""Invariants that must hold for every commit.

1. The package starts no AI CLI process.
2. The package imports no LLM SDK.
3. Source files under the package and its templates contain no non-Latin
   characters; fixtures under tests/ are exempt.

The scanners are written as plain functions over a list of files so that each
one can be pointed at a synthetic violation and proven to catch it. A guard
nobody has seen fail is not a guard (R8).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import lazyfish

PACKAGE_DIR = Path(lazyfish.__file__).parent
REPO_ROOT = PACKAGE_DIR.parent

# Names of AI command line tools. Starting any of these from inside the package
# would move the account decision into the tool, which is precisely the property
# the project promises it does not have.
AI_CLI_NAMES = (
    "claude",
    "codex",
    "gemini",
    "copilot",
    "cursor",
    "aider",
    "goose",
    "ollama",
    "llm",
)

LLM_MODULES = (
    "anthropic",
    "openai",
    "google.generativeai",
    "google_generativeai",
    "vertexai",
    "cohere",
    "mistralai",
    "litellm",
    "langchain",
    "llama_cpp",
    "transformers",
    "ollama",
)

_SUBPROCESS_CALL = re.compile(
    r"(?:subprocess\.(?:run|Popen|call|check_call|check_output)|os\.(?:system|execvp|spawnlp)|"
    r"shutil\.which)\s*\(([^)]*)\)",
    re.DOTALL,
)
_IMPORT = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", re.MULTILINE)

# The only two state constants cli.py has any business naming, both for display:
# one filters the list, the other picks a human-readable label (cli.py:1070 and
# :1075). A fixed allowlist rather than a pattern, so that learning any of the
# states LF-5 adds - PLAN_PROMOTED, REJECTED, ESCALATED - fails here. If the CLI
# needs to show one of those, the label comes from the orchestrator (D13).
CLI_DISPLAY_STATES = frozenset({"STATE_ABANDONED", "STATE_AWAITING_ARTIFACT"})

# Latin Extended-B ends at U+024F. Anything above it is another script, with a
# few punctuation marks that legitimately appear in prose.
_MAX_LATIN_CODEPOINT = 0x024F
_ALLOWED_ABOVE_LATIN = frozenset("‘’“”–—…°×")


def python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


def source_files(directory: Path) -> list[Path]:
    """Python sources plus templates: the English rule covers both."""
    suffixes = {".py", ".j2", ".md", ".json", ".toml", ".txt"}
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts
    )


# --------------------------------------------------------------------------- #
# Scanners
# --------------------------------------------------------------------------- #


def find_ai_subprocess_calls(files: list[Path]) -> list[str]:
    """Report any subprocess-style call whose arguments name an AI CLI."""
    violations = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in _SUBPROCESS_CALL.finditer(text):
            arguments = match.group(1)
            for name in AI_CLI_NAMES:
                # The name must open a quoted argument: "claude", 'claude', or
                # "ollama run llama3". A lookahead keeps "claudette" out.
                if re.search(rf"""['"]{re.escape(name)}(?=['"\s])""", arguments):
                    line = text[: match.start()].count("\n") + 1
                    violations.append(f"{path}:{line}: starts '{name}'")
    return violations


def find_llm_imports(files: list[Path]) -> list[str]:
    violations = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in _IMPORT.finditer(text):
            module = match.group(1)
            root = module.split(".")[0]
            if module in LLM_MODULES or root in LLM_MODULES:
                line = text[: match.start()].count("\n") + 1
                violations.append(f"{path}:{line}: imports '{module}'")
    return violations


def find_transition_judgement(text: str, source: str = "cli.py") -> list[str]:
    """Report signs that state transition judgement has leaked into the CLI.

    LF-5 AC9 and R4: the decision layer is the orchestrator, and cli.py is the
    place it must not reappear. D13 fixes two mechanical criteria:

    1. cli.py does not call check_transition
    2. the state constants cli.py imports do not grow past the two it already
       needs for display

    D13 is explicit that this is a *proxy*, not the property. It catches the
    likely leak - the CLI learning the new states - and misses judgement written
    with the two old constants. That residue is a review item, not a test.
    """
    violations = []
    for number, line in enumerate(text.splitlines(), start=1):
        if re.search(r"\bcheck_transition\s*\(", line):
            violations.append(f"{source}:{number}: calls check_transition")

    # Only what is imported from the state module counts. A name can be used
    # only if it was imported, so the import list is the whole surface - and
    # reading it rather than every STATE_-shaped token keeps constants that
    # merely start with the same word out of it (workspace.STATE_DIRNAME).
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.ImportFrom) or node.module not in ("db", "lazyfish.db"):
            continue
        for alias in node.names:
            if alias.name.startswith("STATE_") and alias.name not in CLI_DISPLAY_STATES:
                violations.append(
                    f"{source}:{node.lineno}: imports {alias.name}, which display does not need"
                )
    return violations


def find_non_latin_characters(files: list[Path]) -> list[str]:
    violations = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            for char in line:
                if ord(char) > _MAX_LATIN_CODEPOINT and char not in _ALLOWED_ABOVE_LATIN:
                    violations.append(
                        f"{path}:{number}: non-Latin character U+{ord(char):04X} ({char!r})"
                    )
                    break
    return violations


# --------------------------------------------------------------------------- #
# The invariants themselves
# --------------------------------------------------------------------------- #


def test_package_starts_no_ai_cli() -> None:
    violations = find_ai_subprocess_calls(python_files(PACKAGE_DIR))
    assert violations == [], "The package must not start an AI CLI. Found:\n" + "\n".join(
        violations
    )


def test_package_imports_no_llm_sdk() -> None:
    violations = find_llm_imports(python_files(PACKAGE_DIR))
    assert violations == [], "The package must not import an LLM SDK. Found:\n" + "\n".join(
        violations
    )


def test_package_and_templates_are_latin_only() -> None:
    violations = find_non_latin_characters(source_files(PACKAGE_DIR))
    assert violations == [], (
        "Sources and templates must be written in English, see CONTRIBUTING.md. "
        "Found:\n" + "\n".join(violations)
    )


def test_project_documentation_is_latin_only() -> None:
    docs = [
        REPO_ROOT / name for name in ("README.md", "CONTRIBUTING.md") if (REPO_ROOT / name).exists()
    ]
    assert find_non_latin_characters(docs) == []


# --------------------------------------------------------------------------- #
# Self-check: the scanners must actually catch violations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "snippet",
    [
        'subprocess.run(["claude"])\n',
        "subprocess.run(['claude', '-p', prompt])\n",
        'subprocess.Popen(["aider", "--yes"])\n',
        'os.system("ollama run llama3")\n',
    ],
)
def test_ai_subprocess_scanner_catches_violations(tmp_path: Path, snippet: str) -> None:
    offender = tmp_path / "offender.py"
    offender.write_text(f"import subprocess\n{snippet}", encoding="utf-8")
    assert find_ai_subprocess_calls([offender])


@pytest.mark.parametrize(
    "snippet",
    [
        "import anthropic\n",
        "from openai import OpenAI\n",
        "import google.generativeai as genai\n",
        "from langchain.chat_models import ChatOpenAI\n",
    ],
)
def test_llm_import_scanner_catches_violations(tmp_path: Path, snippet: str) -> None:
    offender = tmp_path / "offender.py"
    offender.write_text(snippet, encoding="utf-8")
    assert find_llm_imports([offender])


def test_subprocess_scanner_allows_git_and_ripgrep(tmp_path: Path) -> None:
    """The tool does drive git and ripgrep; those must not trip the scanner."""
    allowed = tmp_path / "allowed.py"
    allowed.write_text(
        "import subprocess\n"
        'subprocess.run(["git", "-C", str(repo), "worktree", "add", path])\n'
        'subprocess.run(["rg", "--line-number", "-e", term, "."])\n',
        encoding="utf-8",
    )
    assert find_ai_subprocess_calls([allowed]) == []


def test_non_latin_scanner_catches_a_comment(tmp_path: Path) -> None:
    """AC16: a non-English comment in a source file fails the check."""
    offender = tmp_path / "offender.py"
    offender.write_text(
        "# " + "这是中文注释" + "\nvalue = 1\n",
        encoding="utf-8",
    )
    violations = find_non_latin_characters([offender])
    assert len(violations) == 1
    assert "U+8FD9" in violations[0]


def test_non_latin_scanner_allows_accented_latin(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed.py"
    allowed.write_text("# A naïve café example, 20° warmer\nvalue = 1\n", encoding="utf-8")
    assert find_non_latin_characters([allowed]) == []


def test_the_new_control_modules_are_inside_the_invariant() -> None:
    """LF-5 AC16: the Orchestrator is not an agent, and nothing exempts it.

    The scanners walk the package, so a new module is covered the moment it
    exists. This states it as a criterion because the temptation the handoff
    warns about - letting the driver summarise a failure with one model call -
    lands precisely in these three files.
    """
    scanned = {path.name for path in python_files(PACKAGE_DIR)}
    expected = {"orchestrator.py", "artifacts.py", "rejection.py"}
    assert expected <= scanned, f"not scanned: {sorted(expected - scanned)}"
    assert expected <= {path.name for path in source_files(PACKAGE_DIR)}


def test_the_cli_makes_no_transition_judgement() -> None:
    """LF-5 AC9, first half, as far as it can be mechanised (D13).

    The second half - the orchestrator's decision functions being callable
    without click - is asserted from the other side, in test_orchestrator.py.
    """
    text = (PACKAGE_DIR / "cli.py").read_text(encoding="utf-8")
    assert find_transition_judgement(text) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "from .db import check_transition\ncheck_transition(task.state, STATE_ABANDONED)\n",
        "from .db import STATE_PROMOTED\n",
        "from lazyfish.db import STATE_ABANDONED, STATE_ESCALATED\n",
        "from .db import STATE_REJECTED as WAITING\n",
    ],
)
def test_transition_judgement_scanner_catches_violations(snippet: str) -> None:
    """R8: a guard nobody has seen fail is not a guard."""
    assert find_transition_judgement(snippet) != []


def test_transition_judgement_scanner_allows_display_use() -> None:
    """What cli.py legitimately does today, in the shapes it does it in.

    The two display states, and a same-named constant from another module: the
    criterion is what comes out of db.py, not what a name looks like.
    """
    allowed = (
        "from .db import STATE_ABANDONED, STATE_AWAITING_ARTIFACT, Database\n"
        "from .workspace import STATE_DIRNAME\n"
        "rows = [task for task in tasks if task.state != STATE_ABANDONED]\n"
        "label = 'waiting' if task.state == STATE_AWAITING_ARTIFACT else 'recorded'\n"
    )
    assert find_transition_judgement(allowed) == []


def test_test_fixtures_are_exempt_from_the_latin_rule() -> None:
    """AC16, second half: the same content under tests/ is allowed.

    The i18n fixtures carry Chinese and Japanese ticket text on purpose; the
    rule protects contributors from unreadable source, not from test data.
    """
    fixture_dir = REPO_ROOT / "tests"
    scanned = {path.resolve() for path in source_files(PACKAGE_DIR)}
    assert not any(path.resolve() in scanned for path in fixture_dir.rglob("*") if path.is_file())


# --------------------------------------------------------------------------- #
# The rename is complete (LF-2 AC10)
# --------------------------------------------------------------------------- #


RETIRED_PATTERNS = {
    "repo_profile": re.compile(r"repo_profile"),
    "--repo": re.compile(r"--repo\b"),
    "email_env / token_env": re.compile(r"email_env|token_env"),
    # Added by LF-4. The patterns above are all identifiers, which is exactly
    # why K4 survived them: the retired vocabulary also appears as a TOML table
    # name in error messages and as an English phrase in prose, and neither
    # shape has an underscore or a leading dash to match on.
    "[tracker]": re.compile(r"\[tracker\]"),
    "repo profile": re.compile(r"repo profile"),
    # Added by LF-7. A rename that misses one occurrence produces no error at
    # all: the old name simply stops matching anything, on whichever rare path
    # still spells it. tests/test_migration.py is the one file that must keep
    # them, because translating them is what it tests.
    "old state names": re.compile(r"READY_FOR_PLAN|PLAN_PROMOTED|PLAN_APPROVED"),
}


def repository_text_files() -> list[Path]:
    """Every file a reader of this repository would see, minus build noise."""
    skip_dirs = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        # Working design notes, not distributed and not committed. They quote
        # the pre-rename configuration model on purpose, as a record of what
        # was retired, so scanning them would make this guard fail forever.
        ".design",
    }
    suffixes = {".py", ".j2", ".md", ".json", ".toml", ".yml", ".yaml", ".cfg", ".txt"}
    return sorted(
        path
        for path in REPO_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and not any(part in skip_dirs for part in path.relative_to(REPO_ROOT).parts)
    )


RETIRED_ON_PURPOSE = "retired-vocabulary: on purpose"
"""Marker for the one legitimate reason to write a retired name: translating it.

A migration table has to name what it translates, and a fixture built from an
old schema has to be built from the old schema. Both are the opposite of a
missed rename, so they say so on the line rather than exempting a whole file --
exempting the file would stop guarding everything else in it.
"""


@pytest.mark.parametrize("label", sorted(RETIRED_PATTERNS))
def test_no_trace_of_the_old_configuration_model(label: str) -> None:
    """AC10: an incomplete rename hides until an unusual code path runs (R3).

    The old names are gone from the whole repository, not merely from the code
    paths the other tests happen to exercise. This file names them, so it
    excludes itself, and so does the migration's own test.
    """
    pattern = RETIRED_PATTERNS[label]
    hits = []
    for path in repository_text_files():
        if path.name in ("test_invariants.py", "test_migration.py"):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if RETIRED_ON_PURPOSE in line:
                continue
            if pattern.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert hits == [], f"'{label}' still present:\n" + "\n".join(hits)
