"""Worktree handling and code hints, at the unit level.

The flow tests cover the happy path through the CLI. These cover the awkward
cases underneath it: a branch that already exists, a repository that tracks its
own CLAUDE.md, a machine without ripgrep, and hint output large enough to bury
the ticket text.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lazyfish.config import RepoProfile
from lazyfish.errors import WorkspaceError
from lazyfish.workspace import (
    MAX_HINT_LINES,
    base_branch,
    branch_exists,
    collect_hints,
    create_worktree,
    plan_attachments,
    read_conventions,
    remove_worktree,
    render_context,
)

from .conftest import git, make_ticket


@pytest.fixture
def profile(repo: Path, tmp_path: Path) -> RepoProfile:
    return RepoProfile(
        name="default",
        path=repo,
        conventions=".lazyfish/conventions.md",
        worktree_root=tmp_path / "worktrees",
    )


# --------------------------------------------------------------------------- #
# Worktrees
# --------------------------------------------------------------------------- #


def test_create_worktree_makes_a_branch_and_checkout(profile: RepoProfile) -> None:
    worktree = create_worktree(profile, "PROJ-1")
    assert worktree.created is True
    assert worktree.path.is_dir()
    assert (worktree.path / "src" / "auth" / "reset_token.py").exists()
    assert branch_exists(profile.path, "lazyfish/PROJ-1")


def test_create_worktree_is_idempotent(profile: RepoProfile) -> None:
    first = create_worktree(profile, "PROJ-1")
    second = create_worktree(profile, "PROJ-1")
    assert second.created is False
    assert second.path == first.path


def test_create_worktree_adopts_an_existing_branch(profile: RepoProfile) -> None:
    """A branch left over from an earlier attempt is reused, not duplicated."""
    git(profile.path, "branch", "lazyfish/PROJ-5")
    worktree = create_worktree(profile, "PROJ-5")
    assert worktree.created is True
    assert "lazyfish/PROJ-5" in git(profile.path, "worktree", "list")


def test_create_worktree_refuses_a_foreign_directory(profile: RepoProfile) -> None:
    target = profile.worktree_path("PROJ-1")
    target.mkdir(parents=True)
    (target / "something.txt").write_text("in the way\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="not a git worktree"):
        create_worktree(profile, "PROJ-1")


def test_remove_worktree_reports_each_step(profile: RepoProfile) -> None:
    worktree = create_worktree(profile, "PROJ-1")
    log = remove_worktree(profile, worktree.path, worktree.branch)
    assert not worktree.path.exists()
    assert not branch_exists(profile.path, "lazyfish/PROJ-1")
    assert any("removed worktree" in line for line in log)
    assert any("deleted branch" in line for line in log)


def test_remove_worktree_tolerates_a_missing_directory(profile: RepoProfile) -> None:
    worktree = create_worktree(profile, "PROJ-1")
    shutil.rmtree(worktree.path)
    log = remove_worktree(profile, worktree.path, worktree.branch)
    assert any("already gone" in line for line in log)
    assert not branch_exists(profile.path, "lazyfish/PROJ-1")


def test_base_branch_prefers_explicit_configuration(repo: Path, tmp_path: Path) -> None:
    git(repo, "branch", "release")
    configured = RepoProfile(
        name="default", path=repo, base_branch="release", worktree_root=tmp_path / "w"
    )
    assert base_branch(configured) == "release"


def test_base_branch_falls_back_to_the_checked_out_branch(profile: RepoProfile) -> None:
    """A repository with no remote is normal in tests and in new projects."""
    assert base_branch(profile) == "main"


# --------------------------------------------------------------------------- #
# Conventions
# --------------------------------------------------------------------------- #


def test_read_conventions_returns_none_when_absent(profile: RepoProfile) -> None:
    path, text = read_conventions(profile)
    assert path == profile.path / ".lazyfish" / "conventions.md"
    assert text is None


def test_read_conventions_reads_the_file(profile: RepoProfile) -> None:
    target = profile.path / ".lazyfish" / "conventions.md"
    target.parent.mkdir(parents=True)
    target.write_text("# rules\n", encoding="utf-8")
    _, text = read_conventions(profile)
    assert text == "# rules\n"


# --------------------------------------------------------------------------- #
# Code hints
# --------------------------------------------------------------------------- #


def test_hints_find_a_known_identifier(profile: RepoProfile) -> None:
    worktree = create_worktree(profile, "PROJ-1")
    hints = collect_hints(profile, worktree, ["RESET_TOKEN_TTL"])
    assert hints.lines
    assert any("reset_token.py" in line for line in hints.lines)


def test_hints_can_be_switched_off(profile: RepoProfile) -> None:
    worktree = create_worktree(profile, "PROJ-1")
    hints = collect_hints(profile, worktree, ["RESET_TOKEN_TTL"], enabled=False)
    assert hints.lines == []
    assert hints.enabled is False


def test_hints_respect_search_globs(repo: Path, tmp_path: Path) -> None:
    narrowed = RepoProfile(
        name="default",
        path=repo,
        search_globs=("*.md",),
        worktree_root=tmp_path / "worktrees",
    )
    worktree = create_worktree(narrowed, "PROJ-1")
    hints = collect_hints(narrowed, worktree, ["RESET_TOKEN_TTL"])
    assert all(".py" not in line for line in hints.lines)


def test_hints_are_truncated(profile: RepoProfile) -> None:
    """A hint block long enough to bury the ticket text is worse than none (R2)."""
    worktree = create_worktree(profile, "PROJ-1")
    for index in range(MAX_HINT_LINES + 20):
        (worktree.path / f"noise_{index}.py").write_text("needle = 1\n", encoding="utf-8")
    hints = collect_hints(profile, worktree, ["needle"])
    assert len(hints.lines) == MAX_HINT_LINES
    assert hints.truncated is True


def test_the_python_fallback_matches_ripgrep_on_a_simple_case(
    profile: RepoProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ripgrep installed the tool still produces hints, just slower."""
    worktree = create_worktree(profile, "PROJ-1")
    monkeypatch.setattr("lazyfish.workspace._ripgrep_available", lambda: False)
    hints = collect_hints(profile, worktree, ["RESET_TOKEN_TTL"])
    assert hints.tool == "python-fallback"
    assert any("reset_token.py" in line for line in hints.lines)


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_the_ripgrep_path_produces_the_same_shape(profile: RepoProfile) -> None:
    worktree = create_worktree(profile, "PROJ-1")
    hints = collect_hints(profile, worktree, ["RESET_TOKEN_TTL"])
    assert hints.tool == "ripgrep"
    assert all(line.count(":") >= 2 for line in hints.lines)


# --------------------------------------------------------------------------- #
# Context rendering
# --------------------------------------------------------------------------- #


def render(profile: RepoProfile, worktree, **overrides):
    arguments = {
        "worktree": worktree,
        "profile": profile,
        "ticket": make_ticket(),
        "attachments": [],
        "hints": collect_hints(profile, worktree, [], enabled=False),
        "conventions_text": None,
        "conventions_path": None,
    }
    arguments.update(overrides)
    return render_context(**arguments)


def test_a_tracked_context_file_is_preserved(profile: RepoProfile) -> None:
    """Repositories that already have a CLAUDE.md must not lose it.

    The generated context goes first and the committed file is reproduced below
    it, so a commit made from the worktree cannot silently replace the real one.
    """
    (profile.path / "CLAUDE.md").write_text(
        "# House instructions\n\nRun the linter.\n", encoding="utf-8"
    )
    git(profile.path, "add", "CLAUDE.md")
    git(profile.path, "commit", "-q", "-m", "add context file")

    worktree = create_worktree(profile, "PROJ-1")
    render(profile, worktree)
    body = worktree.context_path.read_text(encoding="utf-8")
    assert "Password reset links expire too early" in body
    assert "Run the linter." in body
    assert "reproduced unchanged" in body


def test_rendering_twice_does_not_duplicate_the_tracked_file(
    profile: RepoProfile,
) -> None:
    (profile.path / "CLAUDE.md").write_text("# House instructions\n", encoding="utf-8")
    git(profile.path, "add", "CLAUDE.md")
    git(profile.path, "commit", "-q", "-m", "add context file")

    worktree = create_worktree(profile, "PROJ-1")
    render(profile, worktree)
    render(profile, worktree)
    body = worktree.context_path.read_text(encoding="utf-8")
    assert body.count("House instructions") == 1


# --------------------------------------------------------------------------- #
# Attachment policy
# --------------------------------------------------------------------------- #


def test_attachment_policy_is_an_allowlist() -> None:
    from lazyfish.config import TrackerConfig
    from lazyfish.trackers.base import Attachment

    tracker = TrackerConfig(
        kind="jira-cloud",
        base_url="https://example.atlassian.net",
        email_env="LAZYFISH_EMAIL",
        token_env="LAZYFISH_TOKEN",
        query="assignee = currentUser()",
        attachment_max_bytes=1000,
    )
    ticket = make_ticket(
        attachments=(
            Attachment("notes.txt", "text/plain", 100, "https://example/1"),
            Attachment("shot.png", "image/png", 100, "https://example/2"),
            Attachment("big.txt", "text/plain", 5000, "https://example/3"),
            Attachment("nourl.txt", "text/plain", 10, ""),
            # A charset parameter must not defeat the allowlist.
            Attachment("utf.txt", "text/plain; charset=utf-8", 10, "https://example/4"),
        )
    )
    plan = plan_attachments(ticket, tracker)
    assert [item.filename for item in plan.to_inline] == ["notes.txt", "utf.txt"]
    reasons = {name.filename: reason for name, reason in plan.skipped}
    assert "allowlist" in reasons["shot.png"]
    assert "exceeds" in reasons["big.txt"]
    assert "no download URL" in reasons["nourl.txt"]
