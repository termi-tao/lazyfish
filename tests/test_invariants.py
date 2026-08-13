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


@pytest.mark.parametrize("label", sorted(RETIRED_PATTERNS))
def test_no_trace_of_the_old_configuration_model(label: str) -> None:
    """AC10: an incomplete rename hides until an unusual code path runs (R3).

    The old names are gone from the whole repository, not merely from the code
    paths the other tests happen to exercise. This file names them, so it
    excludes itself.
    """
    pattern = RETIRED_PATTERNS[label]
    hits = []
    for path in repository_text_files():
        if path.name == "test_invariants.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert hits == [], f"'{label}' still present:\n" + "\n".join(hits)
