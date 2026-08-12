# lazyfish

**A command line tool that turns an issue-tracker ticket into a prepared git
worktree with the ticket's full context on disk, then validates and records what
came out of the design phase.** It does not call a language model and does not
launch an AI tool: it prepares the ground, steps out of the way, and measures
the result.

Two associations the name may trigger, cleared up front: this is not a terminal
UI in the style of `lazygit`, and it has nothing to do with the fish shell.

## Why

Working a ticket by hand means reading the tracker, creating a branch,
re-explaining the requirements and the project's conventions to whatever tool
you design with, and leaving no trace of what was decided. That is repetitive,
and worse, it is unmeasurable: you cannot answer "how much of the plan did I
actually change?"

lazyfish makes the repetitive half deterministic and records the answer to that
question.

## What it does

```
lazyfish prep     tracker -> git worktree + context files -> local database
      |
      v
  (you: cd into the worktree and run whatever design tool you use)
      |
      v
lazyfish show     read the plan, highlight what still needs deciding
lazyfish accept   validate it, record whether you took it as written
```

## What it deliberately does not do

- **No model calls.** The package contains no LLM SDK and starts no AI process.
  A test enforces this, so it cannot drift.
- **No opinion about your stack.** Test runner, linter, directory layout and
  naming rules come from a file in your repository, not from lazyfish.
- **No secret storage.** No keyring, no credentials in the config file. It reads
  environment variables you set, and refuses to start if it finds something that
  looks like a token in the config file.

## Install

```sh
pipx install lazyfish     # or: uv tool install lazyfish
```

Requires Python 3.11+ and `git`. [ripgrep](https://github.com/BurntSushi/ripgrep)
is optional; without it the code search falls back to a slower built-in scan.

## Configure

```sh
lazyfish init
```

It asks six questions - your Atlassian site, your account email, the query that
selects candidate tickets, a name for this repo profile, the local path to the
repository, and an optional reminder - then writes
`~/.config/lazyfish/config.toml`:

```toml
[tracker]
kind = "jira-cloud"
base_url = "https://your-org.atlassian.net"
query = "assignee = currentUser() AND sprint in openSprints() AND status = \"Ready for Dev\" ORDER BY priority DESC, created ASC"

[repo.default]
path = "~/dev/your-repo"
```

No credentials appear in that file, and lazyfish will refuse to start if any
turn up in it. Put them in your shell profile instead:

```sh
export LAZYFISH_EMAIL='you@example.com'
export LAZYFISH_TOKEN='<your API token>'
```

Create the token at **id.atlassian.com -> Security -> API tokens**.

Those two variable names are the defaults, so the config file does not mention
them. If you need different ones - two Atlassian sites, two sets of credentials -
name them explicitly:

```toml
[tracker]
email_env = "WORK_EMAIL"
token_env = "WORK_TOKEN"
```

The query is entirely yours. lazyfish has no built-in project key and no
built-in status name, because workflow states differ between teams: "To Do",
"Sprint Ready" and "Ready for Dev" are all real, and hard-coding any of them
would break everyone else on their first run.

### More than one repository

```toml
[repo.default]
path = "~/dev/service-api"

[repo.frontend]
path = "~/dev/web-client"
search_globs = ["*.ts", "*.tsx"]
account_note = "work seat - check which account your AI client is signed in to"
```

Select one with `lazyfish --repo frontend prep`. Each profile keeps its own
in-flight ticket and its own statistics.

### Repo profile keys

| Key | Default | Meaning |
| --- | --- | --- |
| `path` | required | The git checkout worktrees are created from. |
| `conventions` | `.lazyfish/conventions.md` | File injected verbatim into the prepared context. Repo-relative or absolute. Set to `""` to disable. |
| `search_globs` | all files | Narrows the mechanical code search. |
| `account_note` | none | Printed by every `prep`, unchanged. |
| `base_branch` | remote default | Branch new worktrees start from. |
| `branch_prefix` | `lazyfish/` | Prefix for created branches. |
| `worktree_root` | `~/.local/share/lazyfish/worktrees` | Where worktrees are created. |

### Tracker keys

| Key | Default | Meaning |
| --- | --- | --- |
| `kind` | required | Only `jira-cloud` today. |
| `base_url` | required | Your Jira Cloud site. |
| `query` | required | JQL selecting candidate tickets, best first. |
| `email_env` | `LAZYFISH_EMAIL` | Name of the variable holding your account email. |
| `token_env` | `LAZYFISH_TOKEN` | Name of the variable holding your API token. |
| `attachment_max_bytes` | `100000` | Attachments larger than this are recorded but not downloaded. |
| `attachment_mime_allowlist` | text-like types | Only these are ever written to disk. |
| `timeout_seconds` | `30` | HTTP timeout. |

## Project conventions

The context lazyfish prepares is only as good as what your repository tells it.
Put the things a competent stranger would not guess into
`.lazyfish/conventions.md` and lazyfish will inject it verbatim, never edited:

```markdown
## Conventions a newcomer gets wrong
- Times are stored in UTC and converted only at the presentation layer.
- Money is integer minor units. There is no float anywhere near a price.

## Do not touch without asking
- `src/billing/` needs sign-off from the payments owner.
```

`lazyfish init` can drop a starter version into your repo. If the file is
missing, that section is simply left out and `prep` says so once.

## A full run

```sh
$ lazyfish prep
Candidates (5):
 * 1. PROJ-412  [High]  Password reset email links expire too early
   2. PROJ-398  [Medium]  Add pagination to the audit log endpoint
   ...
Prepared PROJ-412: Password reset email links expire too early
  repo profile  default
  branch        lazyfish/PROJ-412
  worktree      ~/.local/share/lazyfish/worktrees/default/PROJ-412
  context       .../PROJ-412/CLAUDE.md
  design brief  .../PROJ-412/.lazyfish/plan-prompt.md
  write plan to .../PROJ-412/.lazyfish/plan.json

Open the worktree, run the AI tool of your choice, then 'lazyfish show' and 'lazyfish accept'.
cd /home/you/.local/share/lazyfish/worktrees/default/PROJ-412
```

`CLAUDE.md` in that worktree holds the ticket text, its comments, the
attachments that were small and text-like enough to inline, your conventions
file, and a clearly labelled block of mechanical search hits.

Design however you like, write `.lazyfish/plan.json`, then:

```sh
$ lazyfish show      # what the plan says, and what it still leaves open
$ lazyfish accept
Accept the plan for PROJ-412 exactly as written? [Y/n]: n
What did you change, or what was missing?: missed the rate limiter on the reset endpoint
Recorded PROJ-412 as accepted with changes.
```

That answer is the point of the whole exercise. Over a few weeks:

```sh
$ lazyfish status
[default]
  prepared          11
  accepted as-is    4
  accepted modified 6
  as-is rate        40%
  avg prep->accept  23.4 minutes
```

If a ticket turns out to be a bad fit, `lazyfish abandon` removes the worktree
and the branch and frees the profile for the next one.

## Commands

| Command | What it does |
| --- | --- |
| `lazyfish init` | Write the config file, create the database, optionally check the connection. |
| `lazyfish prep` | Choose a ticket, create the worktree, write the context files. Idempotent. |
| `lazyfish show` | Print the current plan and highlight open questions. |
| `lazyfish accept` | Validate the plan and record whether it was taken as written. |
| `lazyfish abandon` | Drop the ticket in flight, delete its worktree and branch. |
| `lazyfish status` | Acceptance rates and cycle time, grouped by repo profile. |

`--repo <profile>` is available on all of them. One ticket per profile is in
flight at a time.

## What the plan file must contain

`accept` validates `.lazyfish/plan.json` against a JSON Schema (written into
each worktree, and reproduced in the design brief), plus three rules a schema
cannot express:

1. `assumptions`, `alternatives_considered` and `open_questions` are each
   non-empty. An empty array means the question was skipped.
2. `needs_human` must be `true` when `confidence` is `low` or any open question
   remains.
3. Every `changes[].file` that is not being created must exist in the worktree.

If a rule is wrong for your situation, `lazyfish accept --force` records the
plan anyway and writes the bypass into the task notes, so the statistics stay
honest.

## Credentials, data and privacy

- **Credentials** live in environment variables. lazyfish reads them, sends them
  to your tracker over HTTPS, and stores them nowhere. Pasting a token into
  `config.toml` is a startup error, not a warning.
- **Ticket data lands on your disk.** Each worktree gets
  `artifacts/<KEY>/ticket.json` with the full ticket, its comments and any
  inlined attachments. lazyfish writes a `.gitignore` next to it so a stray
  `git add -A` cannot commit it, but you should keep these directories out of
  backups you do not control.
- **Attachments** are downloaded only when the mime type is on the allowlist and
  the file is under the size limit. Everything else is recorded as metadata and
  left in the tracker. `prep` prints the filenames before fetching anything.
- **The local database** (`~/.local/share/lazyfish/lazyfish.db`) holds ticket
  keys, titles, timestamps and your acceptance notes. Nothing leaves your
  machine.

## Which AI account gets used

lazyfish never calls a model, so it does not choose, store, or detect any AI
account. Whichever account your design tool is signed in to is the account that
runs; lazyfish cannot see it and cannot change it. This is a structural
property, not a policy: the invariant that the package starts no AI process
leaves account selection entirely outside the tool.

The tool cannot protect you from your own client's global sign-in state, and it
does not try to inspect it, which would be both fragile and tie this tool to one
vendor. What it offers instead is `account_note`: a string on each repo profile,
printed unchanged by every `prep`, to remind you which context you are in.

## Adding another tracker

`trackers/base.py` defines the data model and the `TrackerClient` protocol;
`trackers/jira_cloud.py` is the only implementation today. A new tracker is a new
file plus one line in `trackers/__init__.py`. Nothing outside that package knows
which tracker is in use. Contributions welcome, especially with real instances
to test against.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). One rule to know before you start: all
source, comments, documentation and user-facing strings are in English, and CI
enforces it. Ticket content pulled from your tracker is the exact opposite: it
is passed through character for character, in whatever language it was written,
and lazyfish must never translate or normalise it.

## License

[Apache-2.0](LICENSE).
