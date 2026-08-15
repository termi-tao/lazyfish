"""Command line interface. The only module that writes to the terminal.

Output discipline, because it is load-bearing for scripting and for AC6:

* stdout carries the result of the command - the prepared block, the status
  table, the plan summary. It is deterministic: running `prep` again for an
  already prepared ticket reproduces byte-identical stdout.
* stderr carries everything else - candidate listings, warnings, progress, the
  attachment download notice. All of it depends on the state of the tracker at
  the moment of the call, so none of it belongs in the deterministic stream.

The last line of a successful `prep` is a bare `cd <path>`, so that
`eval "$(lazyfish prep | tail -1)"` works.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import click
import tomlkit

from . import __version__
from .artifacts import (
    CONTRACTS,
    PROMOTER_HUMAN,
    PROMOTER_ORCHESTRATOR,
    ROLE_ORCHESTRATOR,
    TYPE_REJECTION,
    TYPE_TECHNICAL_PLAN,
    Artifact,
    ArtifactStore,
    content_id,
)
from .authority import CALL_SITE_ARCHITECT
from .config import (
    DEFAULT_ATTACHMENT_MAX_BYTES,
    DEFAULT_ATTACHMENT_MIME_ALLOWLIST,
    DEFAULT_BRANCH_PREFIX,
    DEFAULT_CONVENTIONS_PATH,
    DEFAULT_TIMEOUT_SECONDS,
    PROFILE_ENV,
    Config,
    Credentials,
    Profile,
    load_config,
    resolve_credentials,
)
from .db import (
    APPROVAL_INTERACTIVE,
    APPROVAL_NON_INTERACTIVE,
    STATE_ABANDONED,
    STATE_AWAITING_ARTIFACT,
    Database,
    Task,
    open_db,
)
from .errors import LazyfishError, WorkspaceError
from .keywords import extract_keywords
from .orchestrator import (
    PromotionDecision,
    advance_stage,
    decide_promotion,
    ensure_promotable,
    next_step,
    requires_approval,
    state_label,
)
from .paths import config_path, credentials_path, data_home, db_path
from .rejection import Finding, RejectionArtifact, from_human
from .rendering import Column, render_table
from .schema import PlanIssue, load_plan, validate_plan
from .trackers import build_client
from .trackers.base import Ticket
from .workspace import (
    PLAN_FILENAME,
    PROMPT_FILENAME,
    STATE_DIRNAME,
    AttachmentPlan,
    Drift,
    Worktree,
    base_branch,
    collect_hints,
    create_worktree,
    download_attachments,
    measure_drift,
    plan_attachments,
    read_conventions,
    remove_ticket_workspaces,
    render_context,
    run_git,
    ticket_root,
    write_ticket_json,
)

DEFAULT_QUERY = (
    "assignee = currentUser() AND statusCategory != Done ORDER BY priority DESC, created ASC"
)

# Matches what people paste when they read "repository": a clone URL. The single
# slash is not a typo - click.Path and Path() both collapse "https://" to
# "https:/", so the check has to accept the collapsed form too.
REMOTE_URL_RE = re.compile(r"^(?:https?|ssh|git|git\+ssh)://?|^git@|^[\w.-]+:[\w./-]+\.git$")


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def out(message: str = "") -> None:
    """Deterministic result stream."""
    click.echo(message)


def note(message: str) -> None:
    """Informational, non-deterministic stream."""
    click.echo(message, err=True)


def warn(message: str) -> None:
    click.echo(f"warning: {message}", err=True)


def _field(label: str, value: str) -> str:
    return f"  {label:<18}{value}"


# --------------------------------------------------------------------------- #
# Shared plumbing
# --------------------------------------------------------------------------- #


def _config(ctx: click.Context) -> Config:
    return load_config(ctx.obj.get("config_path"))


def _profile(ctx: click.Context, config: Config) -> Profile:
    """--profile, then $LAZYFISH_PROFILE, then default_profile."""
    return config.select(ctx.obj.get("profile"))


def _credentials(ctx: click.Context, profile: Profile) -> Credentials:
    """Credentials for one profile, from the file the group layer settled on."""
    return resolve_credentials(profile.name, ctx.obj.get("credentials_path"))


def _database(ctx: click.Context) -> Database:
    path = ctx.obj.get("db_path") or db_path()
    database = Database(path)
    database.initialise()
    return database


def _worktree_of(task: Task) -> Worktree:
    """Rebuild the Worktree description from a stored row."""
    return Worktree(
        path=Path(task.worktree_path),
        branch=task.branch,
        artifacts_dir=Path(task.artifacts_path),
        created=False,
    )


def _artifact_store() -> ArtifactStore:
    """Where artifact content lives: the data directory, never the repository (D8)."""
    return ArtifactStore(data_home() / "artifacts")


class _Rejected(Exception):
    """A promotion that did not pass. Carries the decision to the command layer."""

    def __init__(self, decision: PromotionDecision):
        super().__init__("the plan did not pass its contract")
        self.decision = decision


TICKET_OPTION_HELP = "Which ticket to act on, when the profile has more than one live."


def _ambiguous(profile: Profile, live: list[Task]) -> LazyfishError:
    """The refusal every command shares when more than one ticket is live (D10).

    Listing the candidates is the point. A command that guessed would be right
    most of the time and catastrophic the rest, and `abandon` is the one that
    makes this concrete: it deletes a worktree and a branch, and a wrong guess
    there cannot be undone.
    """
    candidates = "\n".join(f"  {task.ticket_key}  {task.ticket_title}" for task in live)
    return LazyfishError(
        f"Profile '{profile.name}' has {len(live)} tickets in flight, so this "
        f"command does not know which one you mean:\n{candidates}\n"
        f"Name one with --ticket <KEY>."
    )


def _task_awaiting_decision(
    database: Database, profile: Profile, ticket_key: str | None = None
) -> Task:
    """The task a promotion or an approval applies to.

    Not `get_in_flight`, which only ever returns a task awaiting a plan: the
    window between prep and approval used to be one state and is now several, so
    a promoted or rejected task is still the one being worked on. Whether a given
    state can actually be promoted from is the Orchestrator's answer, not this
    lookup's.

    Several tickets may now be live at once (D8), so this is also where the
    ambiguity is resolved -- by refusing to resolve it. One candidate is not
    ambiguous and behaves exactly as it did before; more than one needs
    `--ticket`; the same shape `Config.select` already uses for profiles.
    """
    live = database.get_live(profile.name)
    if ticket_key:
        for task in live:
            if task.ticket_key == ticket_key:
                return task
        known = ", ".join(task.ticket_key for task in live) or "(none)"
        raise LazyfishError(
            f"{ticket_key} has no live task in profile '{profile.name}'. In flight: {known}."
        )
    if len(live) == 1:
        return live[0]
    if live:
        raise _ambiguous(profile, live)

    recent = database.get_open(profile.name)
    if recent is not None and recent.accepted_at is not None:
        raise LazyfishError(
            f"The plan for {recent.ticket_key} was already recorded on "
            f"{recent.accepted_at}. Run 'lazyfish prep' to start the next ticket."
        )
    raise LazyfishError(
        f"No ticket awaiting a plan for profile '{profile.name}'. Run 'lazyfish prep' first."
    )


def _baseline_for(profile: Profile, task: Task, worktree: Worktree) -> tuple[str, bool]:
    """The commit promotion judges against. Returns (baseline, was_derived).

    Read from the database, not from git: HEAD, refs and the index are all
    writable by the party being checked, so a baseline taken from them could be
    moved by the thing it is supposed to pin down (D4).

    Rows written before this column existed have none. Those do not skip the
    check -- the one ticket actually in flight when the column arrived is the one
    that most needs checking -- so the baseline is derived with merge-base and
    the derivation is announced. Only a failed derivation refuses (AC11).
    """
    if task.base_commit:
        return task.base_commit, False

    base = base_branch(profile)
    result = run_git(worktree.path, "merge-base", "HEAD", base, check=False)
    derived = result.stdout.strip() if result.returncode == 0 else ""
    if not derived:
        raise WorkspaceError(
            f"No recorded baseline for {task.ticket_key}, and one could not be derived.\n"
            f"'git merge-base HEAD {base}' failed in {worktree.path}.\n"
            f"Set base_branch in the profile to a branch this worktree shares "
            f"history with, then run promote again."
        )
    return derived, True


def _previous_findings(
    database: Database, store: ArtifactStore, task_id: int
) -> tuple[Finding, ...]:
    """Findings from the most recent rejection, for the repeat-failure rule (S4).

    Read back from the store rather than kept in memory: a retry is a separate
    process, so "the same rule as last time" is only answerable from what was
    persisted.
    """
    rejections = [item for item in database.list_artifacts(task_id) if item.type == TYPE_REJECTION]
    if not rejections:
        return ()
    content = store.load(task_id, rejections[-1].id)
    return tuple(
        Finding(rule=str(item.get("rule", "")), evidence=str(item.get("evidence", "")))
        for item in content.get("findings", [])
    )


def _store_rejection(
    database: Database,
    store: ArtifactStore,
    task: Task,
    rejection: RejectionArtifact,
    *,
    baseline: str | None,
) -> Artifact:
    """Persist a rejection as an artifact of its own.

    Recorded like any other artifact so that the retry loop leaves a trail that
    can be counted and routed. `parents` names the plan it judged, which is what
    keeps an old rejection true about the version it actually saw.
    """
    content = rejection.to_dict()
    artifact = Artifact(
        id=content_id(content),
        type=TYPE_REJECTION,
        produced_by=ROLE_ORCHESTRATOR,
        # No call site, and that is not an omission: a rejection is the
        # Orchestrator's record of its own deterministic judgement, so no agent
        # call site produces one and the authority table has nothing to say
        # about it. `Rejection` is outside GOVERNED_TYPES for the same reason.
        task_id=task.id,
        base_commit=baseline,
        parents=(rejection.target_artifact,),
        attempt=rejection.attempt,
    )
    recorded = database.record_artifact(artifact)
    store.store(recorded, content)
    return recorded


def _prepared_block(profile: Profile, task: Task, missing_conventions: Path | None) -> str:
    """The deterministic stdout of `prep`, also reprinted on a repeat run."""
    worktree = Path(task.worktree_path)
    lines = [
        f"Prepared {task.ticket_key}: {task.ticket_title}",
        _field("profile", profile.name),
        _field("branch", task.branch),
        _field("worktree", str(worktree)),
        _field("context", str(worktree / "CLAUDE.md")),
        _field("design brief", str(worktree / STATE_DIRNAME / PROMPT_FILENAME)),
        _field("write plan to", str(worktree / STATE_DIRNAME / PLAN_FILENAME)),
    ]
    if profile.account_note:
        lines.append(_field("account note", profile.account_note))
    if missing_conventions is not None:
        lines.append(
            _field(
                "conventions",
                f"not found at {missing_conventions} - the project conventions "
                f"section was left out of CLAUDE.md",
            )
        )
    lines.extend(
        [
            "",
            "Open the worktree, run the AI tool of your choice, then "
            "'lazyfish show' and 'lazyfish accept'.",
            f"cd {worktree}",
        ]
    )
    return "\n".join(lines)


def _missing_conventions(profile: Profile) -> Path | None:
    path, text = read_conventions(profile)
    return path if (path is not None and text is None) else None


# --------------------------------------------------------------------------- #
# Root group
# --------------------------------------------------------------------------- #


class LazyfishGroup(click.Group):
    """Converts LazyfishError into a click error before it can become a traceback.

    Doing it here rather than only in `main` means the same behaviour applies
    when the group is invoked programmatically, which is how the tests check
    that no failure path ever shows a stack trace (AC13).
    """

    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        except LazyfishError as exc:
            error = click.ClickException(str(exc))
            error.exit_code = exc.exit_code
            raise error from exc


@click.group(cls=LazyfishGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="lazyfish")
@click.option(
    "--profile",
    "profile",
    default=None,
    metavar="NAME",
    help=f"Profile from the config file. Falls back to ${PROFILE_ENV}, then default_profile.",
)
@click.option(
    "--config",
    "config_file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Path to config.toml. Defaults to the standard location.",
)
@click.pass_context
def cli(ctx: click.Context, profile: str | None, config_file: Path | None) -> None:
    """Turn a tracker ticket into a prepared git worktree, and record what came back.

    lazyfish never calls a model and never launches an AI tool. It prepares
    context, gets out of the way, then validates and records the result.
    """
    ctx.ensure_object(dict)
    ctx.obj["profile"] = profile
    ctx.obj["config_path"] = config_file
    # Both files are located once, here, so that every reader below agrees on
    # where they are. --config used to move config.toml without moving the
    # credentials beside it: init wrote the token to one directory and the next
    # command looked in another.
    ctx.obj["credentials_path"] = credentials_path(config_file)


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def _load_document(path: Path) -> tomlkit.TOMLDocument:
    """Parse a TOML file for editing, keeping comments and key order.

    tomlkit rather than a plain writer, because `init` edits a file the user
    owns: a second run must not silently delete the explanatory comments the
    first run wrote, nor reorder what they added by hand.
    """
    if not path.exists():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except Exception as exc:  # tomlkit raises several parse error types
        raise LazyfishError(
            f"{path} is not valid TOML and cannot be edited automatically: {exc}\n"
            f"Fix the file by hand, then run 'lazyfish init' again."
        ) from exc


def _write_document(path: Path, document: tomlkit.TOMLDocument, *, secret: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    if secret and os.name == "posix":
        path.chmod(0o600)


def _default_defaults_table() -> tomlkit.items.Table:
    """The [defaults] table written on first run: the built-in values, spelled out.

    Written rather than left implicit so the keys are discoverable. The values
    are exactly what the code falls back to, so deleting the table changes
    nothing.
    """
    allowlist = tomlkit.array()
    allowlist.extend(DEFAULT_ATTACHMENT_MIME_ALLOWLIST)
    allowlist.multiline(True)

    table = tomlkit.table()
    table.add(tomlkit.comment("Inherited by every profile; any profile may override any of these."))
    table.add("timeout_seconds", int(DEFAULT_TIMEOUT_SECONDS))
    table.add("attachment_max_bytes", DEFAULT_ATTACHMENT_MAX_BYTES)
    table.add("attachment_mime_allowlist", allowlist)
    table.add("branch_prefix", DEFAULT_BRANCH_PREFIX)
    return table


def _profile_table(
    *, base_url: str, query: str, repo: Path, account_note: str | None
) -> tomlkit.items.Table:
    """One [profile.<name>] table.

    Optional keys the wizard does not ask about are written as commented-out
    examples rather than bare prose, so that every comment in the file sits
    directly above the key it describes and nothing dangles.
    """
    table = tomlkit.table()
    table.add("tracker", "jira-cloud")
    table.add("base_url", base_url)
    table.add("query", query)
    table.add("repo", str(repo))
    if account_note:
        table.add("account_note", account_note)
    table.add(
        tomlkit.comment('Path to this repo\'s conventions; "" switches the section off entirely.')
    )
    table.add("conventions", DEFAULT_CONVENTIONS_PATH)
    table.add(tomlkit.comment("Optional. Narrows the mechanical code search:"))
    table.add(tomlkit.comment('search_globs = ["*.py", "*.ts"]'))
    return table


@cli.command()
@click.option("--profile", "profile_name", default=None, help="Name for the profile to create.")
@click.option("--base-url", default=None, help="Atlassian site base URL.")
@click.option("--email", default=None, help="Atlassian account email.")
@click.option("--api-token", default=None, help="Atlassian API token.")
@click.option("--query", default=None, help="JQL query selecting candidate tickets.")
@click.option(
    "--repository",
    "repo_path",
    default=None,
    metavar="PATH",
    help="Local path to the target git repository.",
)
@click.option("--account-note", default=None, help="Reminder printed by every prep.")
@click.option("--force", is_flag=True, help="Overwrite a profile of the same name.")
@click.option(
    "--write-conventions/--no-write-conventions",
    default=None,
    help="Copy the conventions example into the target repository.",
)
@click.option("--check/--no-check", default=None, help="Run a tracker connectivity check.")
@click.option("--yes", is_flag=True, help="Do not prompt; use the given options.")
@click.pass_context
def init(
    ctx: click.Context,
    profile_name: str | None,
    base_url: str | None,
    email: str | None,
    api_token: str | None,
    query: str | None,
    repo_path: str | None,
    account_note: str | None,
    force: bool,
    write_conventions: bool | None,
    check: bool | None,
    yes: bool,
) -> None:
    """Add a profile: its project settings and its credentials.

    Run it once per project. A second run appends a new profile and leaves
    everything already in the files untouched.
    """
    config_target = ctx.obj.get("config_path") or config_path()
    credentials_target = ctx.obj.get("credentials_path") or credentials_path()

    interactive = not yes
    if interactive:
        note("lazyfish init: seven answers and one profile is configured.\n")
        # The wording of these seven questions is specified, not improvised.
        profile_name = profile_name or click.prompt("Profile name", default="work")
        # No default for the base URL on purpose: accepting "your-org" on an
        # empty Enter would write a config that points nowhere and fail much
        # later, as a connection error.
        while not base_url:
            answer = click.prompt(
                "Atlassian site base URL (e.g. https://your-org.atlassian.net)"
            ).strip()
            if answer.startswith(("http://", "https://")):
                base_url = answer
            else:
                warn("that should start with https:// - try again")
        email = email or click.prompt("Atlassian account email (the one you log in to Jira with)")
        api_token = api_token or click.prompt(
            "Atlassian API token (id.atlassian.com -> Security -> API tokens)",
            hide_input=True,
        )
        query = query or click.prompt(
            "JQL query for selecting candidate tickets", default=DEFAULT_QUERY
        )
        repo_path = repo_path or click.prompt(
            "Local path to the target git repository (not a remote URL)", type=str
        )
        if account_note is None:
            account_note = (
                click.prompt(
                    "Optional reminder to print on every prep (blank for none)",
                    default="",
                    show_default=False,
                )
                or None
            )

    profile_name = (profile_name or "work").strip()
    missing = [
        name
        for name, value in (
            ("--base-url", base_url),
            ("--email", email),
            ("--api-token", api_token),
            ("--repository", repo_path),
        )
        if not value
    ]
    if missing:
        raise LazyfishError(f"{', '.join(missing)} are required when running with --yes.")

    raw_repo = str(repo_path).strip()
    if REMOTE_URL_RE.match(raw_repo):
        raise LazyfishError(
            f"{raw_repo} is a remote URL, not a local path.\n"
            f"lazyfish creates git worktrees, so it needs a checkout that already "
            f"exists on this machine, for example ~/dev/your-repo.\n"
            f"Clone it first, then point lazyfish at the clone."
        )
    resolved_repo = Path(raw_repo).expanduser()
    if not (resolved_repo / ".git").exists():
        raise LazyfishError(
            f"{resolved_repo} is not a git repository (no .git found). "
            f"lazyfish creates worktrees, so it needs a real checkout."
        )

    document = _load_document(config_target)
    existing = document.get("profile")
    if existing is not None and profile_name in existing and not force:
        raise LazyfishError(
            f"Profile '{profile_name}' already exists in {config_target}.\n"
            f"Choose another name, or pass --force to replace that one profile. "
            f"Everything else in the file is left alone either way."
        )

    profile_table = _profile_table(
        base_url=base_url.rstrip("/"),
        query=query or DEFAULT_QUERY,
        repo=resolved_repo,
        account_note=account_note,
    )
    first_profile = "default_profile" not in document

    if not len(document):
        # A fresh file is built in the order TOML wants it read: the bare key
        # first (bare keys must precede every table), then defaults, then the
        # profiles. A super table renders as [profile.<name>].
        document = tomlkit.document()
        document.add(tomlkit.comment("lazyfish configuration. No credentials here - they live in"))
        document.add(tomlkit.comment(f"the credentials file beside it: {credentials_target}"))
        document.add("default_profile", profile_name)
        document["defaults"] = _default_defaults_table()
        document["profile"] = tomlkit.table(True)
    else:
        if "profile" not in document:
            document["profile"] = tomlkit.table(True)
        if "defaults" not in document:
            document["defaults"] = _default_defaults_table()
        if first_profile:
            # A bare key appended after a table would be parsed as belonging to
            # that table, so the document has to be rebuilt with it in front.
            # Only reached when someone deleted default_profile by hand.
            rebuilt = tomlkit.document()
            rebuilt.add("default_profile", profile_name)
            for key, value in document.items():
                rebuilt[key] = value
            document = rebuilt

    if profile_name in document["profile"]:
        # --force. Update the keys the wizard owns rather than replacing the
        # table: a replacement would also drop any comment or extra key sitting
        # in it, including one the user appended after it (AC18).
        target = document["profile"][profile_name]
        for key, value in profile_table.value.items():
            target[key] = value
        if not account_note and "account_note" in target:
            del target["account_note"]
    else:
        document["profile"][profile_name] = profile_table
    _write_document(config_target, document, secret=False)

    secrets = _load_document(credentials_target)
    section = tomlkit.table()
    section.add("email", email)
    section.add("api_token", api_token)
    secrets[profile_name] = section
    _write_document(credentials_target, secrets, secret=True)

    database_path = ctx.obj.get("db_path") or db_path()
    with open_db(database_path):
        pass

    if write_conventions is None and interactive:
        write_conventions = click.confirm(
            f"Copy the conventions example to {resolved_repo}/{DEFAULT_CONVENTIONS_PATH}?",
            default=False,
        )
    if write_conventions:
        destination = resolved_repo / DEFAULT_CONVENTIONS_PATH
        if destination.exists():
            warn(f"{destination} already exists; left untouched.")
        else:
            example = (Path(__file__).parent / "templates" / "conventions.example.md").read_text(
                encoding="utf-8"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(example, encoding="utf-8")
            out(f"Wrote {destination} - replace its contents with your own rules.")

    verb = "Created" if first_profile else "Added"
    out(f"{verb} profile '{profile_name}'.")
    out(_field("settings", f"{config_target}  (no secrets; safe to share)"))
    out(_field("credentials", f"{credentials_target}  (email and API token, mode 600)"))
    out(_field("database", str(database_path)))
    if first_profile:
        out(_field("default profile", profile_name))
    out("")
    out("Run it with:")
    out(f"    lazyfish --profile {profile_name} prep")
    if first_profile:
        out("")
        out(f"'{profile_name}' is the default, so plain 'lazyfish prep' uses it too.")

    if check is None and interactive:
        check = click.confirm("\nCheck the tracker connection now?", default=True)
    if check:
        _connectivity_check(ctx, profile_name)


def _connectivity_check(ctx: click.Context, profile_name: str) -> None:
    """Fetch one ticket and show how it was parsed.

    Worth the extra step: an instance with unusual field configuration should
    fail here, with the parsed values on screen, rather than midway through the
    first prep (R5).
    """
    note("")
    try:
        config = _config(ctx)
        profile = config.select(profile_name)
        client = build_client(profile, _credentials(ctx, profile))
    except LazyfishError as exc:
        warn(f"connectivity check skipped: {exc}")
        return
    try:
        candidates = client.list_candidates(limit=1)
    except LazyfishError as exc:
        warn(f"connectivity check failed: {exc}")
        return
    finally:
        client.close()

    if not candidates:
        note("Connection works, but the query matched no tickets.")
        return
    ticket = candidates[0]
    note("Connection works. First candidate parsed as:")
    note(f"  key      : {ticket.key}")
    note(f"  title    : {ticket.title}")
    note(f"  status   : {ticket.status}")
    note(f"  priority : {ticket.priority}")
    note(f"  assignee : {ticket.assignee}")


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #

MAX_LIST_LIMIT = 100
"""One page. The tracker client issues a single request, and Jira Cloud caps a
search page at 100 results, so asking for more would silently return fewer."""


def _known_states(ctx: click.Context, profile_name: str, keys: list[str]) -> dict[str, str]:
    """Local state for each key, read-only.

    A missing database is not created here. `list` promises to leave the disk
    exactly as it found it (AC1), and that has to include not bringing a
    database into existence as a side effect of looking at the queue.
    """
    path = ctx.obj.get("db_path") or db_path()
    if not path.exists():
        return {}
    database = Database(path)
    try:
        return database.get_states_for(keys, profile_name)
    finally:
        database.close()


@cli.command("list")
@click.option(
    "--limit",
    default=20,
    show_default=True,
    help=f"How many tickets to list (1 to {MAX_LIST_LIMIT}).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@click.pass_context
def list_tickets(ctx: click.Context, limit: int, as_json: bool) -> None:
    """List the tickets the current profile's query matches.

    Read-only: no worktree, no branch, no database row, nothing written
    anywhere. Run it as often as you like, then start on one of the results
    with 'lazyfish prep --ticket <KEY>'.
    """
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise LazyfishError(
            f"--limit must be between 1 and {MAX_LIST_LIMIT}; got {limit}.\n"
            f"The tracker returns one page per request, and {MAX_LIST_LIMIT} is "
            f"the largest page it will send."
        )

    config = _config(ctx)
    profile = _profile(ctx, config)
    # Deliberately no require_repo() and no in-flight check: this command never
    # touches the checkout, and having a ticket in flight is exactly when you
    # most want to see what else is queued (R3, AC9).

    client = build_client(profile, _credentials(ctx, profile))
    try:
        tickets = client.list_candidates(limit=limit)
    finally:
        client.close()

    states = _known_states(ctx, profile.name, [ticket.key for ticket in tickets])

    if as_json:
        out(
            json.dumps(
                [
                    {
                        "key": ticket.key,
                        "summary": ticket.title,
                        "priority": ticket.priority,
                        "status": ticket.status,
                        "is_known": ticket.key in states,
                        "known_state": states.get(ticket.key),
                    }
                    for ticket in tickets
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    out(_field("PROFILE", profile.name))
    if profile.account_note:
        out(_field("", profile.account_note))
    out(_field("QUERY", profile.query))
    out("")

    if not tickets:
        out("No tickets matched. Widen the query in your profile, or check that")
        out("the tickets you expect are in the state the query asks for.")
        return

    rows = [
        [
            ("* " if ticket.key in states else "  ") + ticket.key,
            ticket.priority or "-",
            ticket.status or "-",
            ticket.title,
        ]
        for ticket in tickets
    ]
    for line in render_table(
        [
            Column("  KEY"),
            Column("PRI"),
            Column("STATUS"),
            Column("SUMMARY", max_width=60),
        ],
        rows,
    ):
        out(line)

    out("")
    if states:
        seen = ", ".join(
            f"{key} ({state.replace('_', ' ').lower()})" for key, state in sorted(states.items())
        )
        out(f"*  already tracked locally: {seen}")
    count = len(tickets)
    out(
        f"{count} ticket{'s' if count != 1 else ''}. "
        f"Start on one with 'lazyfish prep --ticket <KEY>'."
    )


# --------------------------------------------------------------------------- #
# prep
# --------------------------------------------------------------------------- #


@cli.command()
@click.option("--ticket", "ticket_key", default=None, help="Prepare this ticket key.")
@click.option("--limit", default=5, show_default=True, help="Candidates to list.")
@click.option("--no-hints", is_flag=True, help="Skip the mechanical code search.")
@click.pass_context
def prep(ctx: click.Context, ticket_key: str | None, limit: int, no_hints: bool) -> None:
    """Pick a ticket, build its worktree, and write the context files."""
    config = _config(ctx)
    profile = _profile(ctx, config)
    profile.require_repo()

    with _database(ctx) as database:
        # Two intentions, and the difference between them is the whole point of
        # D10. A bare `prep` means "the one I am on": it reprints the single live
        # ticket rather than starting anything, which is what keeps it idempotent
        # (AC15). `prep --ticket K` means "I know I am opening another one", and
        # opening a second ticket has to be something a person said, not
        # something that happened because a query returned a different row today.
        live = database.get_live(profile.name)
        if ticket_key:
            for task in live:
                if task.ticket_key == ticket_key:
                    _handle_active(profile, task)
                    return
        elif len(live) == 1:
            _handle_active(profile, live[0])
            return
        elif live:
            raise _ambiguous(profile, live)

        client = build_client(profile, _credentials(ctx, profile))
        try:
            candidates = client.list_candidates(limit=limit)
            if ticket_key:
                was_top_pick = bool(candidates) and candidates[0].key == ticket_key
            else:
                if not candidates:
                    raise LazyfishError(
                        "The tracker query matched no tickets.\n"
                        f"Check the query in [profile.{profile.name}], or pass "
                        "--ticket <KEY> to prepare a specific one."
                    )
                ticket_key = candidates[0].key
                was_top_pick = True

            _print_candidates(candidates, ticket_key)
            note(f"Fetching {ticket_key} ...")
            ticket = client.fetch(ticket_key)

            _refuse_branch_collision(config, profile, ticket.key)
            worktree = create_worktree(profile, ticket.key)
            write_ticket_json(worktree, ticket)

            attachment_plan = plan_attachments(ticket, profile)
            _print_attachment_plan(attachment_plan)
            attachments = download_attachments(client, worktree, attachment_plan)
        finally:
            client.close()

        conventions_path, conventions_text = read_conventions(profile)
        terms = extract_keywords(
            ticket.title,
            ticket.description,
            *[comment.body for comment in ticket.comments],
        )
        hints = collect_hints(profile, worktree, terms, enabled=not no_hints)

        render_context(
            worktree=worktree,
            profile=profile,
            ticket=ticket,
            attachments=attachments,
            hints=hints,
            conventions_text=conventions_text,
            conventions_path=conventions_path,
        )

        # The baseline is recorded now, while nothing has run in the workspace
        # yet, and it is read from the source repository rather than from the
        # workspace: from here on, everything in there is writable by whatever
        # runs inside it (D4).
        baseline = run_git(profile.repo, "rev-parse", base_branch(profile)).stdout.strip()

        task = database.insert_task(
            ticket_key=ticket.key,
            ticket_title=ticket.title,
            profile=profile.name,
            branch=worktree.branch,
            worktree_path=str(worktree.path),
            artifacts_path=str(worktree.artifacts_dir),
            was_top_pick=was_top_pick,
            base_commit=baseline,
        )

    missing = conventions_path if conventions_text is None else None
    out(_prepared_block(profile, task, missing))


def _branch_holder(repo: Path, branch: str) -> Path | None:
    """The worktree that currently has `branch` checked out, if any."""
    listing = run_git(repo, "worktree", "list", "--porcelain", check=False)
    if listing.returncode != 0:
        return None
    path: Path | None = None
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def _refuse_branch_collision(config: Config, profile: Profile, ticket_key: str) -> None:
    """Say why two profiles cannot prepare the same ticket in the same repository.

    Worktrees are namespaced by profile and branches are not, so two profiles
    whose queries both match one ticket in one repository ask git for a branch
    that is already checked out somewhere else. Git's own refusal is accurate and
    unhelpful -- it names a path under a directory the person has probably never
    looked at, and says nothing about profiles or about the key that fixes it.

    The default branch name is deliberately left alone (D3): the collision needs
    one ticket, two profiles and one repository at once, and lengthening every
    branch name for everyone is the wrong trade. What was missing was not a
    different name, it was an explanation.
    """
    branch = profile.branch_name(ticket_key)
    holder = _branch_holder(profile.repo, branch)
    if holder is None:
        return
    mine = ticket_root(profile, ticket_key)
    if holder == mine or mine in holder.parents:
        return

    # A plain loop, not next(): this module defines a command named `next`, which
    # shadows the builtin for the whole file.
    owner = str(holder)
    for name, candidate in config.profiles.items():
        if name == profile.name or candidate.repo is None:
            continue
        if holder.is_relative_to(candidate.worktree_path(ticket_key)):
            owner = f"profile '{name}'"
            break
    raise WorkspaceError(
        f"Branch {branch} is already checked out by {owner}, so profile "
        f"'{profile.name}' cannot prepare {ticket_key} in the same repository.\n"
        f"Worktrees are kept per profile but branch names are not, and both "
        f"profiles build {branch} for this ticket.\n"
        f"Give one of them its own namespace, for example:\n"
        f"    [profile.{profile.name}]\n"
        f'    branch_prefix = "lazyfish/{profile.name}/"\n'
        f"Or finish the ticket in the other profile first."
    )


def _handle_active(profile: Profile, active: Task) -> None:
    """Repeat run: change nothing, reproduce the same stdout (AC6, AC7).

    The explanation goes to stderr precisely so that stdout stays identical to
    the first run and a script wrapping `prep` keeps working.
    """
    note(
        f"{active.ticket_key} is already prepared for profile "
        f"'{profile.name}' and is waiting for its plan; nothing to do."
    )
    out(_prepared_block(profile, active, _missing_conventions(profile)))


def _print_candidates(candidates: list[Ticket], chosen: str) -> None:
    if not candidates:
        return
    note(f"Candidates ({len(candidates)}):")
    for index, candidate in enumerate(candidates, start=1):
        marker = "*" if candidate.key == chosen else " "
        priority = candidate.priority or "-"
        note(f" {marker} {index}. {candidate.key}  [{priority}]  {candidate.title}")
    if not any(candidate.key == chosen for candidate in candidates):
        note(f" * {chosen} (chosen explicitly, not in the candidate list)")


def _print_attachment_plan(plan: AttachmentPlan) -> None:
    for attachment, reason in plan.skipped:
        note(f"Attachment not downloaded: {attachment.filename} ({reason})")
    if plan.to_inline:
        names = ", ".join(item.filename for item in plan.to_inline)
        note(f"Downloading attachment(s) into the worktree: {names}")


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Print the raw plan as JSON.")
@click.option("--ticket", "ticket_key", default=None, help=TICKET_OPTION_HELP)
@click.pass_context
def show(ctx: click.Context, as_json: bool, ticket_key: str | None) -> None:
    """Print the current plan and highlight what still needs a decision."""
    config = _config(ctx)
    profile = _profile(ctx, config)

    with _database(ctx) as database:
        # Reading the plan is what the approval gate is for, so this has to work
        # while the plan is waiting for approval, not only before it was promoted.
        task = _task_awaiting_decision(database, profile, ticket_key)
        worktree = _worktree_of(task)
        plan = load_plan(worktree.plan_path)

        if as_json:
            out(json.dumps(plan, ensure_ascii=False, indent=2))
            return

        issues = validate_plan(plan, worktree.path)
        out(f"{task.ticket_key}: {task.ticket_title}")
        out(_field("plan", str(worktree.plan_path)))
        out(_field("confidence", str(plan.get("confidence", "unstated"))))
        out(_field("needs human", str(plan.get("needs_human", "unstated"))))
        out("")

        understanding = plan.get("understanding")
        if isinstance(understanding, str) and understanding.strip():
            out("Understanding")
            out(f"  {understanding.strip()}")
            out("")

        _print_list(
            "Open questions",
            [
                f"[{'blocking' if item.get('blocking') else 'non-blocking'}] {item.get('text', '')}"
                for item in plan.get("open_questions", [])
                if isinstance(item, dict)
            ],
        )
        _print_list(
            "Assumptions",
            [item for item in plan.get("assumptions", []) if isinstance(item, str)],
        )
        _print_list(
            "Changes",
            [
                f"{item.get('action', '?'):<7}{item.get('file', '?')}"
                + (
                    f"  (confidence: {item['confidence']})"
                    if item.get("confidence") in ("low", "medium")
                    else ""
                )
                for item in plan.get("changes", [])
                if isinstance(item, dict)
            ],
        )
        _print_list(
            "Acceptance criteria",
            [item for item in plan.get("acceptance_criteria", []) if isinstance(item, str)],
        )

        if issues:
            out(f"Validation: {len(issues)} problem(s). 'lazyfish accept' will refuse:")
            for issue in issues:
                out(f"  {issue}")
        else:
            out("Validation: passes. Run 'lazyfish accept' to record the outcome.")


def _print_list(heading: str, items: list[str]) -> None:
    if not items:
        return
    out(heading)
    for item in items:
        out(f"  - {item}")
    out("")


# --------------------------------------------------------------------------- #
# next / promote: the two commands a driver uses (D9)
# --------------------------------------------------------------------------- #


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Print the answer as JSON.")
@click.option("--ticket", "ticket_key", default=None, help=TICKET_OPTION_HELP)
@click.pass_context
def next(ctx: click.Context, as_json: bool, ticket_key: str | None) -> None:
    """Say what the next step is, and where to take it.

    The same answer for a person and for a driver. In JSON form it is the whole
    interface a runner needs to advance one stage, which is why it is data rather
    than a sentence: the readable format will change, and anything parsing it
    would break (D9).
    """
    config = _config(ctx)
    profile = _profile(ctx, config)

    with _database(ctx) as database:
        # "Nothing in flight" is an answer, not a failure: a runner asks this
        # first and has to be told there is nothing to do without an error. So
        # the ambiguity check only applies once there is something to be
        # ambiguous about.
        live = database.get_live(profile.name)
        if ticket_key or len(live) > 1:
            task = _task_awaiting_decision(database, profile, ticket_key)
        else:
            task = live[0] if live else database.get_open(profile.name)
        step = next_step(task)

    if as_json:
        out(json.dumps(step.to_dict(), ensure_ascii=False))
        return

    if step.state is None:
        out(f"Nothing in flight for profile '{profile.name}'. Run 'lazyfish prep' to start one.")
        return
    if step.blocked_on is not None:
        out(f"Stopped: {state_label(step.state)}.")
        out(_field("reason", step.blocked_on))
        out(_field("ticket", task.ticket_key if task else "-"))
        return
    if step.stage is None:
        out(f"Nothing to do: {state_label(step.state)}.")
        return
    out(f"Next stage: {step.stage}")
    out(_field("ticket", task.ticket_key if task else "-"))
    out(_field("workspace", step.workspace or "-"))
    out(_field("attempt", str(step.attempt)))


def _decide(
    ctx: click.Context,
    database: Database,
    profile: Profile,
    task: Task,
    *,
    force: bool,
) -> tuple[Artifact, str, bool]:
    """Extract, judge and promote the plan. Returns (artifact, state, bypassed).

    This is the whole of AC1 in one place: the plan is read out of the workspace
    by contract and nothing else is looked at. An Architect that also implemented
    the ticket produces exactly the same artifact as one that did not, so those
    changes are not a violation -- they simply do not exist downstream.

    `force` means a person at a terminal chose to record a plan that failed its
    contract. It is deliberately not reachable from `promote`, only from `accept`,
    because anything driving the loop must not be able to skip the check (AC2).
    """
    worktree = _worktree_of(task)
    contract = CONTRACTS[TYPE_TECHNICAL_PLAN]
    store = _artifact_store()

    plan = contract.extract(worktree.path)
    identifier = content_id(plan)

    # Promoting the same content again is a no-op, not a second promotion. The
    # id is the content, so this is decidable without comparing anything else
    # (AC10). Checked before the state guard, because the state that a repeat
    # promotion lands in is exactly the one the guard refuses.
    for existing in database.list_artifacts(task.id):
        if existing.id == identifier and existing.promoted_at is not None:
            return existing, task.state, False

    ensure_promotable(task.state)
    baseline, derived = _baseline_for(profile, task, worktree)
    if derived:
        warn(f"no recorded baseline; derived {baseline[:12]} from merge-base.")

    issues = contract.validate(plan, worktree.path)
    task = database.count_attempt(task.id)

    # Measure what the stage did to the workspace, and record it (C1). This is
    # the last place the workspace has anything to say: promotion ignores it from
    # here on, so the count is taken now or not at all. It is an observation --
    # nothing below reads it, and a failed measurement is a warning, never a
    # refusal, because an observation that can veto is a gate in disguise.
    drift = measure_drift(worktree.path, baseline)
    if drift is None:
        warn("could not measure how far the workspace drifted; recording it as unknown.")
    database.record_drift(task.id, drift.files if drift else None, drift.lines if drift else None)
    artifact = database.record_artifact(
        Artifact(
            id=identifier,
            type=TYPE_TECHNICAL_PLAN,
            produced_by=contract.produced_by,
            # The authority table is keyed by call site, so the only path that
            # promotes anything has to fill it in. Left NULL, every artifact is
            # judged on its role instead -- which is the same answer only while
            # a role has one call site, and silently the wrong one from the
            # moment LF-7 gives the Tester two.
            call_site=CALL_SITE_ARCHITECT,
            task_id=task.id,
            base_commit=baseline,
            attempt=task.attempt,
        )
    )
    store.store(artifact, plan)

    decision = decide_promotion(
        artifact_id=identifier,
        # Forcing says "treat the contract as satisfied": the decision layer is
        # not told a different plan, it is told a different verdict, and who
        # promoted it records that a person made that call.
        issues=[] if force else issues,
        stage_attempts=task.attempt,
        ticket_attempts=task.ticket_attempts,
        previous_findings=_previous_findings(database, store, task.id),
    )

    _report_drift(drift)

    if decision.promoted:
        promoter = PROMOTER_HUMAN if issues else PROMOTER_ORCHESTRATOR
        artifact = database.promote_artifact(task.id, identifier, promoted_by=promoter)
        database.set_state(task.id, decision.next_state)
        # The bypass note is left to the caller: recording the approval rewrites
        # notes, so a note appended here would be overwritten a moment later.
        return artifact, decision.next_state, bool(issues)

    assert decision.rejection is not None  # not promoted implies a rejection
    _store_rejection(database, store, task, decision.rejection, baseline=baseline)
    database.set_state(task.id, decision.next_state, escalation_reason=decision.escalation_reason)
    _report_rejection(decision, issues, worktree.plan_path)
    raise _Rejected(decision)


def _report_drift(drift: Drift | None) -> None:
    """Say what the workspace contains beyond the plan, and that it goes nowhere.

    Printed whether or not there is anything to report, because the useful case
    is the one where the number is large and the person has not noticed. The
    wording says "will not" rather than "must not": those changes are ignored, not
    forbidden, and describing them as a violation would be a different design.
    """
    if drift is None or drift.files == 0:
        return
    note(
        f"the workspace has {drift.files} changed file(s) and {drift.lines} changed "
        f"line(s) besides the plan; none of it will be promoted or reach a later stage."
    )


def _report_rejection(
    decision: PromotionDecision, issues: list[PlanIssue], plan_path: Path
) -> None:
    """Explain a rejection to the person who has to act on it."""
    note(f"{plan_path} does not pass its contract ({len(issues)} problem(s)):")
    for issue in issues:
        note(f"  {issue}")
    note("")
    if decision.escalation_reason:
        note(f"Escalated: {decision.escalation_reason}. A person needs to look at this.")
    elif decision.rejection is not None:
        note(decision.rejection.required_action)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Print the outcome as JSON.")
@click.option("--ticket", "ticket_key", default=None, help=TICKET_OPTION_HELP)
@click.pass_context
def promote(ctx: click.Context, as_json: bool, ticket_key: str | None) -> None:
    """Validate the plan in the workspace and promote it, or reject it.

    There is no flag here that skips validation. Promotion is the Orchestrator's
    authority (D2), and an escape hatch on this command would be reachable by
    anything driving the loop; the one that exists belongs to `accept`, where a
    person is present.

    Promoting is not approving. A promoted plan is waiting for a human decision
    and goes no further on its own (D10, AC13).
    """
    config = _config(ctx)
    profile = _profile(ctx, config)

    with _database(ctx) as database:
        task = _task_awaiting_decision(database, profile, ticket_key)
        try:
            artifact, state, _ = _decide(ctx, database, profile, task, force=False)
        except _Rejected as rejected:
            if as_json:
                out(
                    json.dumps(
                        {
                            "ok": False,
                            "state": rejected.decision.next_state,
                            "artifact": None,
                            "rejection": rejected.decision.rejection.to_dict()
                            if rejected.decision.rejection
                            else None,
                            "escalation_reason": rejected.decision.escalation_reason,
                        },
                        ensure_ascii=False,
                    )
                )
            ctx.exit(6)

    if as_json:
        out(
            json.dumps(
                {
                    "ok": True,
                    "state": state,
                    "artifact": artifact.to_dict(),
                    "rejection": None,
                    "escalation_reason": None,
                },
                ensure_ascii=False,
            )
        )
        return

    out(f"Promoted the plan for {task.ticket_key}.")
    out(_field("artifact", artifact.id[:12]))
    out(_field("baseline", (artifact.base_commit or "-")[:12]))
    out(_field("state", state_label(state)))
    out("")
    out("Read it with 'lazyfish show', then approve it with 'lazyfish accept'.")


# --------------------------------------------------------------------------- #
# accept
# --------------------------------------------------------------------------- #


@cli.command()
@click.option("--as-is", "as_is", is_flag=True, help="Record the plan as accepted unchanged.")
@click.option(
    "--modified",
    "modified",
    is_flag=True,
    help="Record that the plan needed changes; use --note to say what.",
)
@click.option("--note", "note_text", default=None, help="Free text stored with the task.")
@click.option(
    "--force",
    is_flag=True,
    help="Accept despite validation errors. Recorded in the task notes.",
)
@click.option("--reject", "reject", is_flag=True, help="Turn the plan down; use --note to say why.")
@click.option("--ticket", "ticket_key", default=None, help=TICKET_OPTION_HELP)
@click.pass_context
def accept(
    ctx: click.Context,
    as_is: bool,
    modified: bool,
    reject: bool,
    note_text: str | None,
    force: bool,
    ticket_key: str | None,
) -> None:
    """Promote the plan, then record your decision about it.

    Three outcomes, not two (D11). "I accepted it after changing it" and "I am
    turning it down" are different intentions, and the middle one is the bucket
    the project's own judgement table needs: merging them would silently empty
    the 40-70% band.

    The question asked at the prompt and the three flags use the same words, so
    there is one vocabulary to learn rather than two.
    """
    named = [
        flag
        for flag, given in (("--as-is", as_is), ("--modified", modified), ("--reject", reject))
        if given
    ]
    if len(named) > 1:
        raise LazyfishError(
            f"{' and '.join(named)} contradict each other. Each run has one outcome."
        )
    if reject and force:
        raise LazyfishError(
            "--force cannot be combined with --reject.\n"
            "--force means 'record it even though validation failed'. Turning a plan "
            "down needs nothing bypassed: the rejection is the point."
        )

    config = _config(ctx)
    profile = _profile(ctx, config)

    with _database(ctx) as database:
        task = _task_awaiting_decision(database, profile, ticket_key)

        # Whether this stage is one a person approves at all (LF-7 D4). Asked of
        # the authority table rather than worked out from the state: states are
        # stage-independent, so PROMOTED -> APPROVED is a legal edge everywhere,
        # and approving at a stage with no gate would be recorded in silence and
        # then be indistinguishable from a real approval in `plan_accepted`.
        if not requires_approval(task.current_stage):
            raise LazyfishError(
                f"{task.ticket_key} is at the {task.current_stage} stage, which no "
                f"person approves: its artifact advances as soon as it satisfies "
                f"its contract.\n"
                f"Run 'lazyfish promote' to have it judged, or 'lazyfish next' to "
                f"see what the ticket is waiting for."
            )

        # Promote first, always. Approval is a judgement about content, and
        # nobody should be asked to read a plan that does not even satisfy its
        # contract (AC13: the order is fixed).
        try:
            artifact, _, bypassed = _decide(ctx, database, profile, task, force=force)
        except _Rejected:
            ctx.exit(6)

        worktree = _worktree_of(task)
        plan_ticket = load_plan(worktree.plan_path).get("ticket")
        if isinstance(plan_ticket, str) and plan_ticket != task.ticket_key:
            warn(
                f"the plan names ticket {plan_ticket}, but the task in flight is {task.ticket_key}."
            )
        if force:
            warn("plan accepted with --force; the bypass is recorded in the task notes.")

        outcome = named[0] if named else _ask_for_the_outcome(task)
        via = APPROVAL_NON_INTERACTIVE if named else APPROVAL_INTERACTIVE

        if outcome == "--reject":
            _record_rejection_by_hand(database, task, artifact, note_text)
            return

        accepted = outcome == "--as-is"
        notes = note_text
        if not accepted and notes is None:
            notes = _ask_for_a_note("What did you change, or what was missing?")
        # One answer, two columns: the stage moves and `attempt` is cleared
        # together, because a stage that starts with the previous one's spent
        # budget escalates before it has run (LF-7 D5).
        following, next_state = advance_stage(task.current_stage)
        task = database.mark_accepted(
            task.id,
            plan_accepted=accepted,
            next_stage=following,
            next_state=next_state,
            notes=notes or None,
            approved_via=via,
        )
        if bypassed:
            task = database.append_note(task.id, "schema bypassed: the contract did not pass")

        verdict = "as written" if accepted else "with changes"
        out(f"Recorded {task.ticket_key} as accepted {verdict}.")
        minutes = task.minutes_to_accept()
        if minutes is not None:
            out(_field("prep to accept", f"{minutes:.1f} minutes"))
        if task.notes:
            out(_field("notes", task.notes))
        out(_field("worktree", task.worktree_path))


ANSWER_AS_IS = "as-is"
ANSWER_MODIFIED = "modified"
ANSWER_REJECT = "reject"

_OUTCOME_FLAGS = {
    ANSWER_AS_IS: "--as-is",
    ANSWER_MODIFIED: "--modified",
    ANSWER_REJECT: "--reject",
}


def _ask_for_the_outcome(task: Task) -> str:
    """The five-second question. Three choices, and it is never skipped for you.

    This question is where the project's primary measurement comes from, which is
    why approval is never automated: an automated approval would have nowhere to
    ask it.
    """
    answer = click.prompt(
        f"Plan for {task.ticket_key}: accept it {ANSWER_AS_IS}, accept it "
        f"{ANSWER_MODIFIED}, or {ANSWER_REJECT} it?",
        type=click.Choice([ANSWER_AS_IS, ANSWER_MODIFIED, ANSWER_REJECT]),
        default=ANSWER_AS_IS,
        show_choices=True,
    )
    return _OUTCOME_FLAGS[answer]


def _ask_for_a_note(question: str) -> str:
    return click.prompt(question, default="", show_default=False)


def _record_rejection_by_hand(
    database: Database, task: Task, artifact: Artifact, note_text: str | None
) -> None:
    """The third exit: a person turning the plan down (AC15).

    Distinct from a contract rejection in `source`, because a judgement is not a
    rule violation and conflating them would corrupt both counts. The reason
    lands on the task as well as in the artifact: `status` and `show` are this
    slice's only downstream readers, and a reason recorded nowhere a person looks
    is a reason lost (D14).
    """
    reason = note_text if note_text is not None else _ask_for_a_note("Why is it being turned down?")
    # The attempt comes from the artifact, not from the task row read before the
    # promotion: promoting charged an attempt, and the rejection belongs to that
    # attempt rather than to the one before it.
    rejection = from_human(reason, target_artifact=artifact.id, attempt=artifact.attempt)
    _store_rejection(database, _artifact_store(), task, rejection, baseline=artifact.base_commit)
    database.set_state(task.id, STATE_AWAITING_ARTIFACT, notes=reason or None)
    out(f"Turned down the plan for {task.ticket_key}.")
    out(_field("state", state_label(STATE_AWAITING_ARTIFACT)))
    out(_field("attempt", str(artifact.attempt)))
    out("")
    out("Run the design stage again in the same workspace, then 'lazyfish accept'.")


# --------------------------------------------------------------------------- #
# abandon
# --------------------------------------------------------------------------- #


@cli.command()
@click.option("--note", "note_text", default=None, help="Why the ticket was dropped.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.option("--ticket", "ticket_key", default=None, help=TICKET_OPTION_HELP)
@click.pass_context
def abandon(ctx: click.Context, note_text: str | None, yes: bool, ticket_key: str | None) -> None:
    """Drop one ticket: remove every workspace it has, and its branch.

    The command that most needs `--ticket` once several tickets can be live
    (D10). It deletes a worktree and a branch, so a guess it got wrong is not
    something the person can undo -- and unlike the others, its failure is
    silent: the ticket you meant to keep is simply gone.

    Cleanup is per ticket rather than per workspace (R3). A ticket now has one
    directory per call site, and removing only the one the row happens to name
    would leave the rest behind as orphans nothing ever mentions again.
    """
    config = _config(ctx)
    profile = _profile(ctx, config)

    with _database(ctx) as database:
        live = database.get_live(profile.name)
        if ticket_key or len(live) > 1:
            task = _task_awaiting_decision(database, profile, ticket_key)
        else:
            task = live[0] if live else database.get_open(profile.name)
        if task is None:
            raise LazyfishError(
                f"No ticket in flight for profile '{profile.name}'; nothing to abandon."
            )
        profile.require_repo()
        root = ticket_root(profile, task.ticket_key)
        if not yes:
            click.confirm(
                f"Abandon {task.ticket_key} and delete {root} and branch {task.branch}?",
                abort=True,
            )
        log = remove_ticket_workspaces(profile, root, task.branch)
        database.mark_abandoned(task.id, notes=note_text)

    out(f"Abandoned {task.ticket_key}.")
    for entry in log:
        out(f"  {entry}")
    out("Run 'lazyfish prep' to pick up the next ticket.")


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


@cli.command()
@click.option(
    "--all",
    "all_profiles",
    is_flag=True,
    help="Report every profile, ignoring --profile.",
)
@click.pass_context
def status(ctx: click.Context, all_profiles: bool) -> None:
    """Show acceptance rates and cycle time, grouped by profile."""
    config = _config(ctx)
    explicit = ctx.obj.get("profile")
    selected = None if all_profiles or explicit is None else _profile(ctx, config).name

    with _database(ctx) as database:
        groups = database.stats(selected)
        if not groups:
            out("No tasks recorded yet. Run 'lazyfish prep' to start one.")
            return

        for stats in groups:
            out(f"[{stats.profile}]")
            out(_field("prepared", str(stats.prepared)))
            out(_field("in flight", str(stats.in_flight)))
            out(_field("accepted as-is", str(stats.accepted_as_is)))
            out(_field("accepted modified", str(stats.accepted_modified)))
            rate = stats.as_is_rate
            out(
                _field(
                    "as-is rate",
                    f"{rate * 100:.0f}%" if rate is not None else "n/a",
                )
            )
            # Rejections and escalations, counted apart from the acceptances.
            # "Accepted after changes" and "turned down" are different outcomes
            # and merging them would empty the middle band of the judgement
            # table this whole slice exists to fill (AC15).
            out(_field("rejected", str(stats.rejected)))
            out(_field("escalated", str(stats.escalated)))
            out(_field("attempts", str(stats.attempts)))
            # How much the design stage wrote that was never going to travel (C1).
            # A trend, not a threshold: no number here means anything is wrong,
            # and deliberately no line is drawn, because no data supports one yet.
            delta_files = stats.average_delta_files
            delta_lines = stats.average_delta_lines
            out(
                _field(
                    "avg workspace drift",
                    f"{delta_files:.1f} files, {delta_lines:.0f} lines"
                    if delta_files is not None and delta_lines is not None
                    else "n/a",
                )
            )
            out(_field("abandoned", str(stats.abandoned)))
            out(_field("top pick chosen", f"{stats.top_pick_count}/{stats.prepared}"))
            average = stats.average_minutes_to_accept
            out(
                _field(
                    "avg prep->accept",
                    f"{average:.1f} minutes" if average is not None else "n/a",
                )
            )
            out("")

        # Worktrees that still exist: either awaiting a plan, or recorded and
        # presumably being implemented right now.
        open_tasks = [
            task for task in database.list_tasks(selected) if task.state != STATE_ABANDONED
        ]
        if open_tasks:
            out("Open worktrees:")
            for task in open_tasks:
                # The label comes from the orchestrator rather than from a
                # comparison here: this slice adds three states, and a CLI that
                # had to name them in order to display them would blunt the
                # guard that keeps decisions out of the CLI (D13).
                out(
                    f"  {task.profile}: {task.ticket_key} "
                    f"({state_label(task.state)}) {task.worktree_path}"
                )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> int:
    """Console entry point. Turns LazyfishError into a message, never a traceback."""
    try:
        # standalone_mode=False returns the exit code instead of calling
        # sys.exit, which is what lets ctx.exit(6) reach the caller intact.
        result = cli.main(standalone_mode=False, obj={})
        return result if isinstance(result, int) else 0
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        return 130
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except LazyfishError as exc:
        click.echo(f"error: {exc}", err=True)
        return exc.exit_code
    except KeyboardInterrupt:
        click.echo("Interrupted.", err=True)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
