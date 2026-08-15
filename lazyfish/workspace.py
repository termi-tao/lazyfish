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
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import authority
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


def ticket_root(profile: Profile, ticket_key: str) -> Path:
    """Everything belonging to one ticket, one directory per call site inside.

    The extra level is what lets a stage's workspace be kept rather than reset
    (D5): a person can still read what the Architect did after the Coder has
    started, and `abandon` still has one directory to remove for one ticket.
    """
    return profile.worktree_path(ticket_key)


def workspace_path(profile: Profile, ticket_key: str, call_site: str) -> Path:
    """Where one call site's workspace lives: `<profile>/<KEY>/<call site>`."""
    return ticket_root(profile, ticket_key) / call_site


def _refuse_foreign_content(root: Path) -> None:
    """Refuse to use a ticket directory that holds something lazyfish did not put there.

    The same guard `create_worktree` has always applied to the workspace itself,
    one level up: the ticket directory became lazyfish's when D5 put a call site
    inside it, and everything directly in it should therefore be a worktree.
    Writing into a directory a person is using for something else is the failure
    this refuses, and it is worth refusing at the outer level too -- otherwise
    the ticket directory is the one part of the layout nobody checks.
    """
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not (entry / ".git").exists():
            raise WorkspaceError(
                f"{root} holds {entry.name}, which is not a git worktree. lazyfish "
                f"keeps one directory per call site here. Move it aside or point "
                f"worktree_root somewhere else."
            )


def create_worktree(
    profile: Profile, ticket_key: str, call_site: str = authority.CALL_SITE_ARCHITECT
) -> Worktree:
    """Create (or adopt) the worktree and branch for a ticket.

    Idempotent: a second call with an existing worktree returns it untouched,
    which is what makes `lazyfish prep` safe to run repeatedly (AC6). The path
    gained a call-site level in LF-6, and the idempotence did not move with it:
    three bare preps still build one workspace and print the same bytes.
    """
    target = workspace_path(profile, ticket_key, call_site)
    branch = profile.branch_name(ticket_key)
    artifacts = target / ARTIFACTS_DIRNAME / ticket_key

    _refuse_foreign_content(ticket_root(profile, ticket_key))
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


# --------------------------------------------------------------------------- #
# Materialisation (D4, D12)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Workspace:
    """A workspace built from a base commit and a set of promoted artifacts.

    Returned as a value with the inputs named, because "what is in this
    directory" has to be answerable without looking at the directory: the whole
    boundary rests on the content being a function of `base_commit` and
    `applied`, and nothing else.
    """

    path: Path
    call_site: str
    base_commit: str
    applied: tuple[str, ...]
    opened_at: str | None = None
    """The commit this workspace opened as: the baseline plus what it consumes.

    A stage's own work is what came after this, so it is what a diff has to be
    taken against (LF-9 D2). Local to this directory's detached HEAD -- it names
    a starting point, it does not claim any authority `base_commit` has.
    """


def materialize(task: Any, call_site: str) -> Workspace:
    """Build the workspace one call site works in (D4, AC1, AC4, AC5).

        workspace = base_commit + the promoted artifacts this call site consumes

    This function is where every boundary in the design is actually enforced, so
    three things about its shape are load-bearing rather than incidental:

    - **It takes no artifact list.** The signature is `(task, call_site)` and
      nothing else, because a caller that could name the artifacts could name an
      unpromoted one, and doing so would look entirely legitimate. That single
      parameter would bypass "the Coder cannot change the tests" and "only
      promoted content crosses a stage" at the same time (D12). It reads the
      lineage itself.
    - **`consumes` comes from the authority table**, never from a second list
      kept here. Two descriptions of one rule drift, and the drift would be
      invisible: a workspace with one artifact too many still looks fine.
    - **It builds from scratch every time.** The previous contents of the target
      directory are discarded rather than updated, which is what makes the
      result independent of anything an agent left behind (AC4). "Discard" is
      the property under test; reusing a directory would quietly turn it into
      "reset", and a reset that missed a file would be undetectable.

    Nothing here consults HEAD, refs or the index in any workspace: the base
    commit is read from the database, because everything inside a workspace is
    writable by the party being checked.
    """
    # Imported inside the function: db imports artifacts, which imports this
    # module for its filenames, so a module-level import would close a cycle.
    # Materialisation is the one thing in here that needs to read stored state.
    from .artifacts import PATCH_FIELD, ArtifactStore, carries_patch
    from .config import load_config
    from .db import Database
    from .paths import data_home, db_path

    consumes = authority.consumes_for(call_site)
    profile = load_config().select(task.profile)
    profile.require_repo()

    if not task.base_commit:
        raise WorkspaceError(
            f"{task.ticket_key} has no recorded base commit, so a workspace for "
            f"{call_site} cannot be built from one. Abandon the task and prepare "
            f"it again to record a baseline."
        )

    database = Database(db_path())
    try:
        promoted = _promoted_inputs(database.list_artifacts(task.id), consumes)
    finally:
        database.close()

    target = workspace_path(profile, task.ticket_key, call_site)
    _replace_worktree(profile, target, task.base_commit)

    store = ArtifactStore(data_home() / "artifacts")
    applied: list[str] = []
    # Files an earlier artifact in this build already owns. A later patch is not
    # allowed to rewrite them, which is LF-6 AC1 -- "the tests a downstream
    # stage sees are byte for byte the promoted ones" -- finally becoming
    # reachable now that a Coder exists to violate it (LF-9 D3). Enforced here
    # rather than by restricting what the Coder may produce: its patch still
    # records everything it changed, that part simply does not travel.
    spoken_for: set[str] = set()
    for artifact in promoted:
        if not carries_patch(artifact.type):
            continue
        patch = str(store.load(task.id, artifact.id).get(PATCH_FIELD) or "")
        if not patch.strip():
            # Fail closed. A promoted TestArtifact carrying no patch materialises
            # a workspace with no tests in it, and skipping it quietly makes that
            # indistinguishable from a correct build: the artifact is missing
            # from `applied` and nothing is raised. An artifact whose type says
            # it carries a patch and whose content does not is broken, and the
            # contract rule that would have caught it earlier arrives with LF-7.
            raise WorkspaceError(
                f"The promoted {artifact.type} {artifact.id[:12]} carries no patch, "
                f"so the workspace for {call_site} cannot be built from it. "
                f"Promote a replacement, or abandon the task and start again."
            )
        content = store.load(task.id, artifact.id)
        _apply_patch(target, patch, artifact.id, exclude=sorted(spoken_for))
        applied.append(artifact.id)
        spoken_for.update(str(name) for name in content.get("files") or ())

    _write_context_inputs(task, call_site, target, consumes, database_path=db_path())

    return Workspace(
        path=target,
        call_site=call_site,
        base_commit=task.base_commit,
        applied=tuple(applied),
        opened_at=_commit_the_opening_state(target, call_site) if applied else task.base_commit,
    )


def _commit_the_opening_state(target: Path, call_site: str) -> str:
    """Commit what materialisation just built, and return its sha (LF-9 D2).

    A stage whose workspace already holds promoted artifacts cannot diff against
    the bare baseline -- it would present the artifacts it was given as its own
    work. The combined tree needs a name, and committing is how a tree gets one.

    On the detached HEAD materialisation created, so no branch moves and nothing
    is pushed. `.lazyfish/` is excluded for the usual reason: lazyfish wrote it.
    """
    excludes = [f":(exclude){path}" for path in TOOL_WRITTEN_PATHS]
    run_git(target, "add", "-A", "--", ".", *excludes)
    run_git(
        target,
        "-c",
        "user.email=lazyfish@localhost",
        "-c",
        "user.name=lazyfish",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        f"lazyfish: {call_site} workspace as opened",
    )
    return run_git(target, "rev-parse", "HEAD").stdout.strip()


class _TicketStub:
    """What a stage brief needs of a ticket: its key.

    A materialised workspace is built from the database, and the full Ticket
    lives in the tracker's response, which is not re-fetched. The briefs written
    here are told what they can be told.
    """

    def __init__(self, key: str):
        self.key = key


def _ticket_stub(task: Any) -> _TicketStub:
    return _TicketStub(task.ticket_key)


def _profile_of(task: Any):
    from .config import load_config

    return load_config().select(task.profile)


def _write_context_inputs(
    task: Any, call_site: str, target: Path, consumes: tuple[str, ...], *, database_path: Path
) -> None:
    """Write the inputs that are read but never promoted (LF-6, implemented in LF-8).

    The authority table has listed these beside the artifact types since LF-6,
    with a note saying they reach a workspace as context files rather than as a
    patch. This is that note becoming code.

    The distinction is not cosmetic. `acceptance_criteria` is *extracted from*
    the approved plan, and what the Tester gets is the criteria alone -- not the
    plan, not its reasoning, not its list of files to change. A stage that
    consumes an artifact is built from that artifact; a stage that consumes a
    pseudo-input is handed a fact taken out of one.
    """
    from .artifacts import (
        COVERAGE_FILENAME,
        REPORT_FILENAME,
        TYPE_TECHNICAL_PLAN,
        ArtifactStore,
        ac_id_for,
        acceptance_criteria_of,
    )
    from .db import Database
    from .paths import data_home

    state_dir = target / STATE_DIRNAME
    state_dir.mkdir(parents=True, exist_ok=True)

    if authority.INPUT_TICKET in consumes:
        # Copied from the ticket's first workspace rather than re-rendered: it is
        # a file lazyfish wrote, not something an agent produced, so the copy is
        # the same bytes the design stage was given. Re-rendering would mean
        # running the code search again and handing this stage a different
        # context for the same ticket.
        source = ticket_root(_profile_of(task), task.ticket_key) / authority.CALL_SITE_ARCHITECT
        original = source / CONTEXT_FILENAME
        if original.exists() and original.resolve() != (target / CONTEXT_FILENAME).resolve():
            shutil.copyfile(original, target / CONTEXT_FILENAME)

    if authority.INPUT_ACCEPTANCE_CRITERIA in consumes:
        database = Database(database_path)
        try:
            promoted = [
                artifact
                for artifact in database.list_artifacts(task.id)
                if artifact.type == TYPE_TECHNICAL_PLAN and artifact.promoted_at is not None
            ]
        finally:
            database.close()
        # No approved plan is not a reason to refuse a workspace. The criteria
        # are context, and context that can veto is a gate in disguise (C1's
        # rule, applied here). The gate that matters is in the contract: with no
        # criteria, every `ac_id` a coverage table declares is out of range and
        # the promotion fails there, which is the layer that should be saying no.
        criteria: list[str] = []
        if promoted:
            store = ArtifactStore(data_home() / "artifacts")
            criteria = acceptance_criteria_of(store.load(task.id, promoted[-1].id))

        lines = [
            f"# Acceptance criteria for {task.ticket_key}",
            "",
            "From the approved plan. The numbering is what "
            f"`{STATE_DIRNAME}/{COVERAGE_FILENAME}` refers to.",
            "",
        ]
        lines += [f"- **{ac_id_for(index)}** — {text}" for index, text in enumerate(criteria)]
        if not criteria:
            lines.append("_No approved plan carries acceptance criteria for this ticket._")
        (state_dir / ACCEPTANCE_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")

    template = STAGE_BRIEFS.get(call_site)
    if template is not None:
        brief = (
            _environment()
            .get_template(template)
            .render(
                ticket=_ticket_stub(task),
                acceptance_path=f"{STATE_DIRNAME}/{ACCEPTANCE_FILENAME}",
                coverage_path=f"{STATE_DIRNAME}/{COVERAGE_FILENAME}",
                report_path=f"{STATE_DIRNAME}/{REPORT_FILENAME}",
            )
        )
        (state_dir / template.removesuffix(".j2")).write_text(brief, encoding="utf-8")


def _promoted_inputs(artifacts: list[Any], consumes: tuple[str, ...]) -> list[Any]:
    """The latest promoted artifact of each consumed type, in lineage order.

    Latest rather than all of them: a retried stage leaves several artifacts of
    one type behind, and only the one that was promoted last is the version
    downstream is built from. Unpromoted rows are not candidates at all -- that
    is the entire difference between having produced something and having it
    count.
    """
    latest: dict[str, Any] = {}
    for artifact in artifacts:
        if artifact.promoted_at is None or artifact.type not in consumes:
            continue
        latest[artifact.type] = artifact
    return [latest[type_name] for type_name in consumes if type_name in latest]


def _replace_worktree(profile: Profile, target: Path, base_commit: str) -> None:
    """Put a clean checkout of `base_commit` at `target`, replacing what is there.

    Detached rather than on a branch: a materialised workspace is an input to one
    stage, and the promoted artifact carries a patch against the base commit, so
    there is no history for a branch to name. It also means a stage's workspace
    can never collide with the ticket's branch or with another stage's.
    """
    if target.exists():
        run_git(profile.repo, "worktree", "remove", "--force", str(target), check=False)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        run_git(profile.repo, "worktree", "prune", check=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    run_git(profile.repo, "worktree", "add", "--detach", str(target), base_commit)


def _apply_patch(target: Path, patch: str, artifact_id: str, exclude: Sequence[str] = ()) -> None:
    """Apply one artifact's unified diff to a materialised workspace.

    A diff and not a commit, and applied to the working tree rather than merged:
    taking a commit would mean trusting refs inside the workspace an agent was
    given, which is the state most obviously under its control (D4, Q5).

    `exclude` names files an earlier artifact in the same build already owns, so
    that a later stage's patch cannot rewrite an earlier one's contribution
    (LF-9 D3). Empty for the first patch, which is the common case.
    """
    text = patch if patch.endswith("\n") else patch + "\n"
    exclusions = [f"--exclude={path}" for path in exclude]
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "apply", "--whitespace=nowarn", *exclusions, "-"],
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git is checked earlier
        raise WorkspaceError("git was not found on PATH.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WorkspaceError(
            f"The promoted artifact {artifact_id[:12]} does not apply to the "
            f"workspace at {target}:\n{detail}"
        )


@dataclass(frozen=True)
class Drift:
    """How far a workspace moved away from the baseline it was materialised from.

    An observation, never a verdict. Promotion ignores changes in the workspace
    by design; this only counts them, so that "did the design stage also write
    the implementation" becomes a number instead of an impression.
    """

    files: int
    lines: int


# Everything lazyfish itself writes into a workspace. Excluded from the count,
# because a freshly prepared workspace where no agent has typed a character
# already contains all of it -- leaving it in would give every ticket the same
# constant offset and make the measurement useless.
#
# This is not a path boundary. It decides nothing about what may be written and
# refuses nothing; it removes the tool's own output from a measurement of the
# agent's. Taken from the constants above so that renaming an output file keeps
# the exclusion correct.
TOOL_WRITTEN_PATHS = (STATE_DIRNAME, CONTEXT_FILENAME, ARTIFACTS_DIRNAME)

ACCEPTANCE_FILENAME = "acceptance.md"

STAGE_BRIEFS = {
    authority.CALL_SITE_TESTER_WRITE: "tests-prompt.md.j2",
    authority.CALL_SITE_CODER: "impl-prompt.md.j2",
    authority.CALL_SITE_TESTER_VERIFY: "verify-prompt.md.j2",
}
"""The brief each stage's workspace gets, where it has one.

The Architect's is written by `render_context` at prep, because its workspace is
built by `create_worktree` rather than materialised. Every later stage's is
written here. Two paths for the same idea is a wart worth naming; merging them
waits until a second materialised stage makes the shared shape visible.
"""


def diff_since(
    workspace_path: Path, base_commit: str, exclude: Sequence[str] = TOOL_WRITTEN_PATHS
) -> tuple[str, list[str]]:
    """One stage's work as a unified diff against the baseline, and the files in it.

    The same temporary-index technique `measure_drift` uses, and for the same two
    reasons: the repository's own index is never touched, and staging everything
    first is what makes newly created files appear at all. New test files are
    untracked by definition, so a plain `git diff` would return an empty patch
    for a workspace full of tests -- and the promotion would fail with
    "unchanged" rather than with anything true.

    Unlike `measure_drift`, a failure here raises. That one is an observation and
    an observation must never veto; this one produces the artifact, and an
    artifact that could not be built is not a smaller artifact.
    """
    with tempfile.TemporaryDirectory() as scratch:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        excludes = [f":(exclude){path}" for path in exclude]

        staged = subprocess.run(
            ["git", "-C", str(workspace_path), "add", "-A", "--", ".", *excludes],
            capture_output=True,
            text=True,
            env=environment,
        )
        if staged.returncode != 0:
            raise WorkspaceError(
                f"Could not stage the workspace at {workspace_path} to build a patch:\n"
                f"{(staged.stderr or staged.stdout).strip()}"
            )

        def run(*args: str) -> subprocess.CompletedProcess:
            result = subprocess.run(
                ["git", "-C", str(workspace_path), *args],
                capture_output=True,
                text=True,
                env=environment,
            )
            if result.returncode != 0:
                raise WorkspaceError(
                    f"git {' '.join(args)} failed in {workspace_path}:\n"
                    f"{(result.stderr or result.stdout).strip()}"
                )
            return result

        patch = run("diff", "--cached", "--binary", base_commit).stdout
        names = run("diff", "--cached", "--name-only", base_commit).stdout
    return patch, [line for line in names.splitlines() if line.strip()]


def run_tests(workspace_path: Path, command: str, timeout_seconds: float) -> int:
    """Run the repository's own test command in a workspace, and return its code.

    The exit code and nothing else. Reading the output would mean knowing one
    framework's format, and the core does not know any -- what it needs is the
    single fact every runner agrees on: did this pass (LF-9 D4).

    Running a command the user configured is not the invariant's concern. The
    rule is that lazyfish starts no *AI* process; a test suite is the user's own
    tooling, executed in their own repository's terms.

    lazyfish's own environment variables are removed before the run. A suite
    that could see LAZYFISH_DATA_DIR might read or write the tool's state, and a
    test run reaching into the tool measuring it is a loop nobody wants to debug.
    """
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("LAZYFISH_")
    }
    try:
        result = subprocess.run(
            shlex.split(command),
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            env=environment,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorkspaceError(
            f"The configured test_command could not be run: {exc}\n"
            f"Command: {command}\n"
            f"It runs in the workspace, so anything it needs has to be on PATH there."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            f"The test command did not finish within {timeout_seconds:g}s and was "
            f"stopped.\nCommand: {command}\n"
            f"Raise test_timeout_seconds in the profile if the suite is simply slow."
        ) from exc
    return result.returncode


def measure_drift(worktree_path: Path, base_commit: str) -> Drift | None:
    """Count files and lines changed in a workspace relative to `base_commit`.

    Returns None when the measurement could not be taken, which is never a
    reason to refuse a promotion: this is an observation, and an observation that
    can veto is a gate wearing a disguise.

    The comparison runs against a temporary index, so the repository's own index
    is not touched. That is the same rule promotion follows for its baseline --
    a check must not rest on state the party being checked can write -- and it
    also happens to be the tidiest way to count modified and newly created files
    in one pass: stage everything into a throwaway index, then diff it.
    """
    with tempfile.TemporaryDirectory() as scratch:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        excludes = [f":(exclude){path}" for path in TOOL_WRITTEN_PATHS]

        staged = subprocess.run(
            ["git", "-C", str(worktree_path), "add", "-A", "--", ".", *excludes],
            capture_output=True,
            text=True,
            env=environment,
        )
        if staged.returncode != 0:
            return None

        counted = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", "--numstat", "--cached", base_commit],
            capture_output=True,
            text=True,
            env=environment,
        )
        if counted.returncode != 0:
            return None

    files = 0
    lines = 0
    for row in counted.stdout.splitlines():
        parts = row.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        # A binary file reports "-" for both counts. It still moved, so it counts
        # as a file; there is no meaningful line count to add.
        added, deleted = parts[0], parts[1]
        lines += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
    return Drift(files=files, lines=lines)


def _remove_one_worktree(profile: Profile, worktree_path: Path) -> list[str]:
    """Take down one workspace directory, whatever state it is in."""
    if not worktree_path.exists():
        return [f"worktree {worktree_path} was already gone"]
    result = run_git(profile.repo, "worktree", "remove", "--force", str(worktree_path), check=False)
    if result.returncode == 0:
        return [f"removed worktree {worktree_path}"]
    shutil.rmtree(worktree_path, ignore_errors=True)
    return [f"deleted directory {worktree_path} (git worktree remove failed)"]


def _delete_branch(profile: Profile, branch: str) -> list[str]:
    """Delete the ticket's branch, reporting rather than failing."""
    if not branch_exists(profile.repo, branch):
        return [f"branch {branch} did not exist"]
    result = run_git(profile.repo, "branch", "-D", branch, check=False)
    if result.returncode == 0:
        return [f"deleted branch {branch}"]
    return [f"could not delete branch {branch}: {(result.stderr or '').strip()}"]


def remove_worktree(profile: Profile, worktree_path: Path, branch: str) -> list[str]:
    """Tear down a worktree and its branch. Returns a log of what happened.

    Tolerant on purpose: abandon must work even when the worktree was deleted by
    hand or the branch was already merged and removed. A cleanup command that
    can itself get stuck defeats the point (R4).
    """
    log = _remove_one_worktree(profile, worktree_path)
    run_git(profile.repo, "worktree", "prune", check=False)
    return log + _delete_branch(profile, branch)


def remove_ticket_workspaces(profile: Profile, root: Path, branch: str) -> list[str]:
    """Tear down every call-site workspace of one ticket, then its branch (R3).

    Scoped to one ticket's directory, which is what makes abandoning one ticket
    leave the others in the profile untouched (AC16). A ticket has as many
    workspaces as it has reached call sites, and the task row names only the one
    it was prepared at, so removing that alone would leave orphans behind --
    directories git still lists as worktrees and nothing else ever mentions.
    """
    log: list[str] = []
    workspaces = sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []
    if not workspaces:
        log.append(f"worktree {root} was already gone")
    for path in workspaces:
        log.extend(_remove_one_worktree(profile, path))

    run_git(profile.repo, "worktree", "prune", check=False)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    return log + _delete_branch(profile, branch)


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
