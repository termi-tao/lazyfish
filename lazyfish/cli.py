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
from .config import (
    DEFAULT_ATTACHMENT_MAX_BYTES,
    DEFAULT_ATTACHMENT_MIME_ALLOWLIST,
    DEFAULT_BRANCH_PREFIX,
    DEFAULT_CONVENTIONS_PATH,
    DEFAULT_TIMEOUT_SECONDS,
    PROFILE_ENV,
    Config,
    Profile,
    load_config,
    resolve_credentials,
)
from .db import (
    STATE_ABANDONED,
    STATE_READY_FOR_PLAN,
    Database,
    Task,
    open_db,
)
from .errors import LazyfishError
from .keywords import extract_keywords
from .paths import config_path, credentials_path, db_path
from .rendering import Column, render_table
from .schema import PlanIssue, load_plan, validate_plan
from .trackers import build_client
from .trackers.base import Ticket
from .workspace import (
    PLAN_FILENAME,
    PROMPT_FILENAME,
    STATE_DIRNAME,
    AttachmentPlan,
    Worktree,
    collect_hints,
    create_worktree,
    download_attachments,
    plan_attachments,
    read_conventions,
    remove_worktree,
    render_context,
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
    credentials_target = config_target.parent / credentials_path().name

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
        client = build_client(profile, resolve_credentials(profile.name))
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

    client = build_client(profile, resolve_credentials(profile.name))
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
        active = database.get_in_flight(profile.name)
        if active is not None:
            _handle_active(profile, active)
            return

        client = build_client(profile, resolve_credentials(profile.name))
        try:
            candidates = client.list_candidates(limit=limit)
            if ticket_key:
                was_top_pick = bool(candidates) and candidates[0].key == ticket_key
            else:
                if not candidates:
                    raise LazyfishError(
                        "The tracker query matched no tickets.\n"
                        "Check the [tracker] query in your config, or pass "
                        "--ticket <KEY> to prepare a specific one."
                    )
                ticket_key = candidates[0].key
                was_top_pick = True

            _print_candidates(candidates, ticket_key)
            note(f"Fetching {ticket_key} ...")
            ticket = client.fetch(ticket_key)

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

        task = database.insert_task(
            ticket_key=ticket.key,
            ticket_title=ticket.title,
            profile=profile.name,
            branch=worktree.branch,
            worktree_path=str(worktree.path),
            artifacts_path=str(worktree.artifacts_dir),
            was_top_pick=was_top_pick,
        )

    missing = conventions_path if conventions_text is None else None
    out(_prepared_block(profile, task, missing))


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
@click.pass_context
def show(ctx: click.Context, as_json: bool) -> None:
    """Print the current plan and highlight what still needs a decision."""
    config = _config(ctx)
    profile = _profile(ctx, config)

    with _database(ctx) as database:
        task = database.get_in_flight(profile.name)
        if task is None:
            raise LazyfishError(
                f"No ticket awaiting a plan for profile '{profile.name}'. "
                f"Run 'lazyfish prep' first."
            )
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
@click.pass_context
def accept(
    ctx: click.Context,
    as_is: bool,
    modified: bool,
    note_text: str | None,
    force: bool,
) -> None:
    """Validate the plan and record whether it was taken as written."""
    if as_is and modified:
        raise LazyfishError("--as-is and --modified contradict each other.")

    config = _config(ctx)
    profile = _profile(ctx, config)

    with _database(ctx) as database:
        # get_in_flight only ever returns a READY_FOR_PLAN task, so a plan that
        # has already been recorded cannot be accepted twice.
        task = database.get_in_flight(profile.name)
        if task is None:
            recorded = database.get_open(profile.name)
            if recorded is not None:
                raise LazyfishError(
                    f"The plan for {recorded.ticket_key} was already recorded on "
                    f"{recorded.accepted_at}. Run 'lazyfish prep' to start the next "
                    f"ticket."
                )
            raise LazyfishError(
                f"No ticket awaiting a plan for profile '{profile.name}'. "
                f"Run 'lazyfish prep' first."
            )

        worktree = _worktree_of(task)
        plan = load_plan(worktree.plan_path)
        issues = validate_plan(plan, worktree.path)

        if issues and not force:
            _report_issues(issues, worktree.plan_path)
            ctx.exit(6)

        plan_ticket = plan.get("ticket")
        if isinstance(plan_ticket, str) and plan_ticket != task.ticket_key:
            warn(
                f"the plan names ticket {plan_ticket}, but the task in flight is {task.ticket_key}."
            )

        if as_is:
            accepted, notes = True, note_text
        elif modified:
            accepted, notes = False, note_text
        else:
            accepted = click.confirm(
                f"Accept the plan for {task.ticket_key} exactly as written?",
                default=True,
            )
            if accepted:
                notes = note_text
            else:
                notes = note_text or click.prompt(
                    "What did you change, or what was missing?", default="", show_default=False
                )

        task = database.mark_accepted(task.id, plan_accepted=accepted, notes=notes or None)
        if issues and force:
            task = database.append_note(
                task.id, f"schema bypassed: {len(issues)} unresolved validation issue(s)"
            )
            warn("plan accepted with --force; the bypass is recorded in the task notes.")

        verdict = "as written" if accepted else "with changes"
        out(f"Recorded {task.ticket_key} as accepted {verdict}.")
        minutes = task.minutes_to_accept()
        if minutes is not None:
            out(_field("prep to accept", f"{minutes:.1f} minutes"))
        if task.notes:
            out(_field("notes", task.notes))
        out(_field("worktree", task.worktree_path))


def _report_issues(issues: list[PlanIssue], plan_path: Path) -> None:
    note(f"{plan_path} does not pass validation ({len(issues)} problem(s)):")
    for issue in issues:
        note(f"  {issue}")
    note("")
    note("Fix the plan and run 'lazyfish accept' again, or use --force to accept it")
    note("anyway; a forced accept is recorded in the task notes.")


# --------------------------------------------------------------------------- #
# abandon
# --------------------------------------------------------------------------- #


@cli.command()
@click.option("--note", "note_text", default=None, help="Why the ticket was dropped.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_context
def abandon(ctx: click.Context, note_text: str | None, yes: bool) -> None:
    """Drop the ticket in flight: remove its worktree and branch."""
    config = _config(ctx)
    profile = _profile(ctx, config)

    with _database(ctx) as database:
        task = database.get_open(profile.name)
        if task is None:
            raise LazyfishError(
                f"No ticket in flight for profile '{profile.name}'; nothing to abandon."
            )
        if not yes:
            click.confirm(
                f"Abandon {task.ticket_key} and delete {task.worktree_path} "
                f"and branch {task.branch}?",
                abort=True,
            )
        profile.require_repo()
        log = remove_worktree(profile, Path(task.worktree_path), task.branch)
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
                label = "awaiting plan" if task.state == STATE_READY_FOR_PLAN else "plan recorded"
                out(f"  {task.profile}: {task.ticket_key} ({label}) {task.worktree_path}")


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
