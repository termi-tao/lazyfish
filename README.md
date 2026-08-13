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
- **No secret storage.** No keyring. Credentials live in one file of yours at
  mode 600, or in environment variables you set; lazyfish refuses to start if it
  finds something that looks like a token in the shareable config file.

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

Seven questions - profile name, Atlassian site, account email, API token, the
query that selects candidate tickets, the local path to the repository, and an
optional reminder - and you have a working profile. Run it again per project.

lazyfish keeps two files, both in `~/.config/lazyfish/` and nowhere else:

| File | Contains | Shareable |
| --- | --- | --- |
| `config.toml` | profiles: tracker, query, repository, options | **Yes.** Commit it to dotfiles, paste it into a bug report, hand it to a colleague. |
| `credentials` | one section per profile: `email`, `api_token` | **No.** Mode 600, never leaves your machine. |

```toml
default_profile = "work"

[defaults]
timeout_seconds = 30
branch_prefix = "lazyfish/"

[profile.work]
tracker  = "jira-cloud"
base_url = "https://your-org.atlassian.net"
query    = "assignee = currentUser() AND sprint in openSprints() AND status = \"Ready for Dev\""
repo     = "~/dev/service-api"

[profile.infra]
tracker  = "jira-cloud"
base_url = "https://your-org.atlassian.net"
query    = "project = INFRA AND assignee = currentUser()"
repo     = "~/dev/infrastructure"
search_globs = ["*.tf", "*.yaml"]
```

```toml
# ~/.config/lazyfish/credentials, mode 600
[work]
email     = "you@example.com"
api_token = "..."

[infra]
email     = "you@example.com"
api_token = "..."
```

The profile name is the join key between the two files. There is no fuzzy
matching: a profile with no section of the same name in `credentials` is an
error naming both the profile and the file.

Create API tokens at **id.atlassian.com -> Security -> API tokens**. They last
at most 365 days; the day one expires, every command starts failing with a 401
that looks exactly like a misconfiguration, so lazyfish's own message names both
possibilities.

### Why a query belongs to a profile

Each profile carries its query *and* its repository. That pairing is the whole
point of the structure: with a single global query, nothing stops you pulling a
ticket from project A and building its worktree in project B's repository. Here
that mismatch cannot be expressed.

For the same reason, `tracker`, `base_url`, `query` and `repo` are rejected in
`[defaults]`. Everything else may be defaulted.

### Selecting a profile

```sh
lazyfish --profile infra prep     # explicit
LAZYFISH_PROFILE=infra lazyfish prep
lazyfish prep                     # falls back to default_profile
```

`--profile` beats `LAZYFISH_PROFILE`, which beats `default_profile`. Each profile
keeps its own in-flight ticket and its own statistics: **one ticket per profile
at a time**, not one overall. Two profiles means two tickets can be awaiting a
plan simultaneously.

### Defaults and overrides

`[defaults]` sets any non-identity key for every profile; any profile overrides
any of them. The order is **profile value, then `[defaults]`, then the built-in**.

| Key | Built-in | Meaning |
| --- | --- | --- |
| `conventions` | `.lazyfish/conventions.md` | File injected verbatim into the prepared context. Repo-relative or absolute. |
| `search_globs` | all files | Narrows the mechanical code search. |
| `account_note` | none | Printed by every `prep`, unchanged. |
| `base_branch` | the remote's default | Branch new worktrees start from. |
| `branch_prefix` | `lazyfish/` | Prefix for created branches. |
| `worktree_root` | `~/.local/share/lazyfish/worktrees` | Where worktrees are created. |
| `timeout_seconds` | `30` | HTTP timeout. |
| `attachment_max_bytes` | `100000` | Attachments larger than this are recorded but not downloaded. |
| `attachment_mime_allowlist` | text-like types | Only these are ever written to disk. |

What decides an override is **whether the key is present**, never whether its
value looks empty. That matters for two keys, and they point in opposite
directions:

```toml
conventions  = ""     # switches the conventions section OFF (not "use the default")
search_globs = []     # imposes NO filter, i.e. search every file (not "search nothing")
```

Neither falls back to `[defaults]`. The asymmetry is real and worth reading
twice; it is recorded rather than silently smoothed over.

### Where lazyfish looks, and where it does not

Only `~/.config/lazyfish/` (honouring `XDG_CONFIG_HOME`). It does **not** search
the current directory, does not walk up through parent directories, and does not
read anything from the target repository. A `config.toml` sitting in the
directory you happen to run from is ignored completely.

That is deliberate rather than lazy: lazyfish creates git worktrees, and a
worktree is a fresh checkout where ignored files do not appear. Any scheme that
reads configuration out of a repository behaves differently inside the worktrees
this tool exists to create.

You can still point it somewhere else explicitly, which is a different thing from
searching - you name one path and get exactly that path:

```sh
lazyfish --config /path/to/other.toml status
LAZYFISH_CONFIG=/path/to/other.toml lazyfish status
LAZYFISH_DATA_DIR=/path/to/state lazyfish status
```

### Changing it later

Edit the files. Every command re-reads and re-validates them, so there is
nothing to reload:

```sh
$EDITOR ~/.config/lazyfish/config.toml
lazyfish status                 # cheapest check: parses and validates, no network
```

`lazyfish init` is also safe to re-run: it appends a new profile and leaves
everything already in the file - including your own comments and any key you
added by hand - untouched. A name that already exists is refused rather than
overwritten; `--force` replaces that one profile and nothing else.

### Credentials from the environment

`LAZYFISH_EMAIL` and `LAZYFISH_TOKEN` override the file, which is what CI and
containers need:

```sh
export LAZYFISH_EMAIL='you@example.com'
export LAZYFISH_TOKEN='...'
```

With **both** set, the credentials file is never opened - it may be absent, or
wrongly permissioned, without consequence. With only one set, the other still
comes from the file, so the file is still read and still has to be mode 600.

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
  profile           work
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
[work]
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
| `lazyfish init` | Add a profile: write its settings and credentials, create the database. |
| `lazyfish list` | Show the tickets your query matches. Read-only, no side effects. |
| `lazyfish prep` | Choose a ticket, create the worktree, write the context files. Idempotent. |
| `lazyfish show` | Print the current plan and highlight open questions. |
| `lazyfish accept` | Validate the plan and record whether it was taken as written. |
| `lazyfish abandon` | Drop the ticket in flight, delete its worktree and branch. |
| `lazyfish status` | Acceptance rates and cycle time, grouped by profile. |

`--profile <name>` is available on all of them. One ticket per profile is in
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

- **Credentials** live in `~/.config/lazyfish/credentials` at mode 600, or in
  `LAZYFISH_EMAIL` / `LAZYFISH_TOKEN`. lazyfish reads them, sends them to your
  tracker over HTTPS, and stores them nowhere else. Pasting a token into
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
vendor. What it offers instead is `account_note`: a string on each profile,
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
