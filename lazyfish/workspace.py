"""Worktree creation, context files, code hints, cleanup.

Everything that touches the target repository or the local filesystem lives
here. git is driven through subprocess: the tool only needs `worktree add`,
`worktree remove` and `branch -D`, and a library would add a dependency and a
compatibility surface for no gain (A-5).

This module never prints. It returns descriptions of what it did and lets the
CLI decide what the user sees.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .config import Profile
from .errors import WorkspaceError
from .schema import schema_text
from .trackers.base import Attachment, Ticket, TrackerClient

TEMPLATE_DIR = Path(__file__).parent / "templates"

CONTEXT_FILENAME = "CLAUDE.md"
STATE_DIRNAME = ".lazyfish"
PLAN_FILENAME = "plan.json"
PROMPT_FILENAME = "plan-prompt.md"
SCHEMA_FILENAME = "plan-schema.json"
ARTIFACTS_DIRNAME = "artifacts"

MAX_HINT_LINES = 40
MAX_HINT_FILE_BYTES = 512_000

HINT_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ARTIFACTS_DIRNAME,
    }
)


# --------------------------------------------------------------------------- #
# git plumbing
# --------------------------------------------------------------------------- #


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run one git command inside `repo`."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorkspaceError(
            "git was not found on PATH. lazyfish drives git directly and cannot work without it."
        ) from exc
    if check and result.returncode != 0:
        command = " ".join(args)
        detail = (result.stderr or result.stdout or "").strip()
        raise WorkspaceError(f"git {command} failed in {repo}:\n{detail}")
    return result


def base_branch(profile: Profile) -> str:
    """The branch new worktrees start from.

    Explicit config wins; otherwise the remote's default branch; otherwise
    whatever the repository currently has checked out. The last case covers a
    repository with no remote, which is common in tests and in fresh projects.
    """
    if profile.base_branch:
        return profile.base_branch
    result = run_git(
        profile.repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    result = run_git(profile.repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    branch = result.stdout.strip() if result.returncode == 0 else ""
    if not branch or branch == "HEAD":
        raise WorkspaceError(
            f"Cannot determine a base branch for profile '{profile.name}' "
            f"({profile.repo}). Set base_branch in the profile, for example "
            f'base_branch = "main".'
        )
    return branch


def branch_exists(repo: Path, branch: str) -> bool:
    result = run_git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return result.returncode == 0


# --------------------------------------------------------------------------- #
# Worktree lifecycle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Worktree:
    """A prepared working area for one ticket."""

    path: Path
    branch: str
    artifacts_dir: Path
    created: bool

    @property
    def plan_path(self) -> Path:
        return self.path / STATE_DIRNAME / PLAN_FILENAME

    @property
    def context_path(self) -> Path:
        return self.path / CONTEXT_FILENAME


def create_worktree(profile: Profile, ticket_key: str) -> Worktree:
    """Create (or adopt) the worktree and branch for a ticket.

    Idempotent: a second call with an existing worktree returns it untouched,
    which is what makes `lazyfish prep` safe to run repeatedly (AC6).
    """
    target = profile.worktree_path(ticket_key)
    branch = profile.branch_name(ticket_key)
    artifacts = target / ARTIFACTS_DIRNAME / ticket_key

    if target.exists():
        if not (target / ".git").exists():
            raise WorkspaceError(
                f"{target} already exists but is not a git worktree. Move it aside "
                f"or point worktree_root somewhere else."
            )
        return Worktree(path=target, branch=branch, artifacts_dir=artifacts, created=False)

    target.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(profile.repo, branch):
        run_git(profile.repo, "worktree", "add", str(target), branch)
    else:
        run_git(profile.repo, "worktree", "add", "-b", branch, str(target), base_branch(profile))
    return Worktree(path=target, branch=branch, artifacts_dir=artifacts, created=True)


def remove_worktree(profile: Profile, worktree_path: Path, branch: str) -> list[str]:
    """Tear down a worktree and its branch. Returns a log of what happened.

    Tolerant on purpose: abandon must work even when the worktree was deleted by
    hand or the branch was already merged and removed. A cleanup command that
    can itself get stuck defeats the point (R4).
    """
    log: list[str] = []
    if worktree_path.exists():
        result = run_git(
            profile.repo, "worktree", "remove", "--force", str(worktree_path), check=False
        )
        if result.returncode == 0:
            log.append(f"removed worktree {worktree_path}")
        else:
            shutil.rmtree(worktree_path, ignore_errors=True)
            log.append(f"deleted directory {worktree_path} (git worktree remove failed)")
    else:
        log.append(f"worktree {worktree_path} was already gone")

    run_git(profile.repo, "worktree", "prune", check=False)

    if branch_exists(profile.repo, branch):
        result = run_git(profile.repo, "branch", "-D", branch, check=False)
        if result.returncode == 0:
            log.append(f"deleted branch {branch}")
        else:
            log.append(f"could not delete branch {branch}: {(result.stderr or '').strip()}")
    else:
        log.append(f"branch {branch} did not exist")
    return log


# --------------------------------------------------------------------------- #
# Artifacts
# --------------------------------------------------------------------------- #


def _ensure_self_ignoring(directory: Path) -> None:
    """Drop a `.gitignore` containing `*` into a generated directory.

    Cheaper and less intrusive than editing the repository's own ignore rules,
    and it keeps ticket text out of an accidental `git add -A` (R7). An existing
    file is never overwritten.
    """
    marker = directory / ".gitignore"
    if not marker.exists():
        marker.write_text("*\n", encoding="utf-8")


def write_ticket_json(worktree: Worktree, ticket: Ticket) -> Path:
    """Persist the ticket exactly as fetched, including its raw payload."""
    worktree.artifacts_dir.mkdir(parents=True, exist_ok=True)
    _ensure_self_ignoring(worktree.path / ARTIFACTS_DIRNAME)
    target = worktree.artifacts_dir / "ticket.json"
    payload = ticket.to_dict()
    payload["raw"] = ticket.raw
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    return target


@dataclass(frozen=True)
class AttachmentPlan:
    """What would happen to each attachment, decided before any download."""

    to_inline: list[Attachment] = field(default_factory=list)
    skipped: list[tuple[Attachment, str]] = field(default_factory=list)


def plan_attachments(ticket: Ticket, profile: Profile) -> AttachmentPlan:
    """Decide which attachments may be written to disk.

    Allowlist by mime type and cap by size (R1). Nothing is fetched here so the
    CLI can print the list and the user can see it before bytes land on their
    machine.
    """
    plan = AttachmentPlan()
    for attachment in ticket.attachments:
        mime = attachment.mime_type.split(";")[0].strip().lower()
        if mime not in profile.attachment_mime_allowlist:
            plan.skipped.append((attachment, f"mime type {mime} is not in the allowlist"))
        elif attachment.size_bytes > profile.attachment_max_bytes:
            plan.skipped.append(
                (
                    attachment,
                    f"{attachment.size_bytes} bytes exceeds the "
                    f"{profile.attachment_max_bytes} byte limit",
                )
            )
        elif not attachment.url:
            plan.skipped.append((attachment, "the tracker gave no download URL"))
        else:
            plan.to_inline.append(attachment)
    return plan


def download_attachments(
    client: TrackerClient, worktree: Worktree, plan: AttachmentPlan
) -> list[Attachment]:
    """Fetch the allowed attachments and return them with content attached.

    A failure on one attachment degrades to a note on that attachment: a missing
    log file is not a reason to throw away a prepared worktree.
    """
    if not plan.to_inline:
        return []
    directory = worktree.artifacts_dir / "attachments"
    directory.mkdir(parents=True, exist_ok=True)

    downloaded: list[Attachment] = []
    for attachment in plan.to_inline:
        # Keep the tracker's filename verbatim, including non-ASCII characters;
        # only strip any directory component (AC15).
        safe_name = Path(attachment.filename).name or "attachment"
        target = directory / safe_name
        try:
            content = client.download_attachment(attachment)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            downloaded.append(
                Attachment(
                    filename=attachment.filename,
                    mime_type=attachment.mime_type,
                    size_bytes=attachment.size_bytes,
                    url=attachment.url,
                    author=attachment.author,
                    created=attachment.created,
                    inline_text=f"[lazyfish could not download this attachment: {exc}]",
                )
            )
            continue
        target.write_bytes(content)
        downloaded.append(
            Attachment(
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                size_bytes=attachment.size_bytes,
                url=attachment.url,
                author=attachment.author,
                created=attachment.created,
                inline_text=content.decode("utf-8", errors="replace"),
                local_path=str(target),
            )
        )
    return downloaded


# --------------------------------------------------------------------------- #
# Code hints
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Hints:
    """Mechanical search results, plus how they were produced."""

    terms: list[str]
    lines: list[str]
    truncated: bool
    tool: str

    @property
    def enabled(self) -> bool:
        return bool(self.terms)


def _ripgrep_available() -> bool:
    return shutil.which("rg") is not None


def _search_with_ripgrep(root: Path, terms: list[str], globs: tuple[str, ...]) -> list[str]:
    command = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--max-count",
        "3",
        "--max-columns",
        "200",
        "--max-filesize",
        "512K",
    ]
    for glob in globs:
        command.extend(["--glob", glob])
    command.extend(["--glob", f"!{ARTIFACTS_DIRNAME}/"])
    for term in terms:
        command.extend(["-e", term])
    command.append(".")
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True, encoding="utf-8", check=False
    )
    # rg exits 1 when there are no matches, which is not an error here.
    if result.returncode not in (0, 1):
        raise WorkspaceError(f"ripgrep failed: {(result.stderr or '').strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def _search_in_python(root: Path, terms: list[str], globs: tuple[str, ...]) -> list[str]:
    """Fallback used when ripgrep is not installed.

    Slower and less clever, but it keeps the tool usable on a machine without
    rg rather than silently producing no hints.
    """
    matches: list[str] = []
    per_file_cap = 3
    for path in sorted(root.rglob("*")):
        if len(matches) >= MAX_HINT_LINES * 3:
            break
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in HINT_SKIP_DIRS for part in relative.parts):
            continue
        if globs and not any(
            fnmatch.fnmatch(relative.name, glob) or fnmatch.fnmatch(str(relative), glob)
            for glob in globs
        ):
            continue
        try:
            if path.stat().st_size > MAX_HINT_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found = 0
        for number, line in enumerate(text.splitlines(), start=1):
            if found >= per_file_cap:
                break
            if any(term in line for term in terms):
                matches.append(f"{relative}:{number}:{line.strip()[:200]}")
                found += 1
    return matches


def collect_hints(
    profile: Profile, worktree: Worktree, terms: list[str], enabled: bool = True
) -> Hints:
    """Search the worktree for the extracted terms.

    Results are capped and labelled. Over-long hint sections are worse than none
    at all: they push the real ticket text out of view (R2).
    """
    if not enabled or not terms:
        return Hints(terms=[], lines=[], truncated=False, tool="disabled")

    if _ripgrep_available():
        tool = "ripgrep"
        lines = _search_with_ripgrep(worktree.path, terms, profile.search_globs)
    else:
        tool = "python-fallback"
        lines = _search_in_python(worktree.path, terms, profile.search_globs)

    truncated = len(lines) > MAX_HINT_LINES
    return Hints(terms=terms, lines=lines[:MAX_HINT_LINES], truncated=truncated, tool=tool)


# --------------------------------------------------------------------------- #
# Context files
# --------------------------------------------------------------------------- #


def read_conventions(profile: Profile) -> tuple[Path | None, str | None]:
    """Return the configured conventions path and its content, if any.

    Missing is a normal outcome, not an error: the section is omitted and the
    CLI mentions it once (AC3).
    """
    path = profile.conventions_path()
    if path is None or not path.exists() or not path.is_file():
        return path, None
    try:
        return path, path.read_text(encoding="utf-8")
    except OSError:
        return path, None


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        # Control statements on their own line leave no blank line behind. The
        # context file is read by people and by tools that count tokens; stray
        # whitespace costs both.
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )


def _committed_content(repo: Path, relative: str) -> str | None:
    """Content of a tracked file at HEAD, or None if git does not track it.

    Read from git rather than from disk so that re-running prep does not see the
    file lazyfish itself wrote on the previous run.
    """
    if run_git(repo, "ls-files", "--error-unmatch", relative, check=False).returncode != 0:
        return None
    result = run_git(repo, "show", f"HEAD:{relative}", check=False)
    return result.stdout if result.returncode == 0 else None


def render_context(
    *,
    worktree: Worktree,
    profile: Profile,
    ticket: Ticket,
    attachments: list[Attachment],
    hints: Hints,
    conventions_text: str | None,
    conventions_path: Path | None,
) -> list[Path]:
    """Write CLAUDE.md and the design prompt into the worktree."""
    environment = _environment()
    state_dir = worktree.path / STATE_DIRNAME
    state_dir.mkdir(parents=True, exist_ok=True)

    inline_by_name = {item.filename: item for item in attachments}
    merged_attachments = [inline_by_name.get(item.filename, item) for item in ticket.attachments]

    body = environment.get_template("CLAUDE.md.j2").render(
        ticket=ticket,
        profile=profile,
        attachments=merged_attachments,
        hints=hints,
        conventions_text=conventions_text,
        conventions_path=str(conventions_path) if conventions_path else None,
        plan_path=f"{STATE_DIRNAME}/{PLAN_FILENAME}",
        prompt_path=f"{STATE_DIRNAME}/{PROMPT_FILENAME}",
    )

    # A repository may track its own CLAUDE.md. Overwriting it inside the
    # worktree would risk that edit being committed back, so keep both.
    context_path = worktree.context_path
    original = _committed_content(worktree.path, CONTEXT_FILENAME)
    if original:
        body = (
            f"{body}\n\n---\n\n"
            f"## Repository {CONTEXT_FILENAME} (tracked in git, reproduced unchanged)\n\n"
            f"{original}"
        )
    context_path.write_text(body, encoding="utf-8")

    prompt = environment.get_template(PROMPT_FILENAME).render(
        ticket=ticket,
        schema_json=schema_text().rstrip("\n"),
        plan_path=f"{STATE_DIRNAME}/{PLAN_FILENAME}",
    )
    (state_dir / PROMPT_FILENAME).write_text(prompt, encoding="utf-8")
    (state_dir / SCHEMA_FILENAME).write_text(schema_text(), encoding="utf-8")

    return [context_path, state_dir / PROMPT_FILENAME, state_dir / SCHEMA_FILENAME]
