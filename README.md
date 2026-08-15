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
lazyfish prep      tracker -> git worktree + context files -> local database
      |
      v
  (you: cd into the worktree and run whatever design tool you use)
      |
      v
lazyfish promote   check the plan against its contract, keep it as an artifact
      |
      v
lazyfish show      read the plan, highlight what still needs deciding
lazyfish accept    record your decision: as-is, modified, or turned down
```

`accept` promotes for you, so the short version is still `prep`, design, `accept`.

## Only the plan leaves the worktree

The worktree is yours to do anything in. Nothing is locked, and nothing is
policed. What lazyfish keeps when the design stage ends is **the plan file, and
nothing else** — recorded as an artifact, identified by a hash of its content,
alongside the commit it was written against.

That has a consequence worth being explicit about: if the design stage also
writes the implementation, those changes are not a violation and will not fail
anything. They simply do not travel. No later step starts from that directory.

The worktree is not deleted — you can look through it, and `prep` will adopt it
again — but after promotion its contents carry no authority.

What lazyfish does do is **count** what it found there. Promoting reports how many
files and lines changed beyond the plan, and records both against the ticket, so
`lazyfish status` can show you the trend. Files lazyfish wrote itself are not
counted; neither number can fail a promotion, and no threshold is defined. It is
there so that "the design stage keeps writing the implementation" is a figure you
can look up rather than a feeling, and so that a decision about whether to do
anything about it can wait for evidence.

This is why there is no `--force` on `promote`. Checking the plan against its
contract is the tool's job and is deterministic; overriding a failed check is a
person's decision, so that flag lives on `accept`, where a person is present, and
the override is recorded in the task.

**Promoting is not approving.** A promoted plan is waiting for you. Nothing
advances past that point on its own, and nothing is ever pushed or turned into a
pull request for you.

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

Not on PyPI yet. Until the first release, install from the repository:

```sh
pipx install "git+https://github.com/termi-tao/lazyfish.git"
```

A git install has no version to compare, so `pipx upgrade` will report it is
already current after a new commit lands. Use `pipx upgrade lazyfish --force`,
or install with `-e` from a clone and let `git pull` be the upgrade.

Requires Python 3.11+ and `git`. [ripgrep](https://github.com/BurntSushi/ripgrep)
is optional; without it the code search falls back to a slower built-in scan.

## Configure

```sh
lazyfish init
```

Seven questions - profile name, Atlassian site, account email, API token, the
query that selects candidate tickets, the local path to the repository, and an
optional reminder - and you have a working profile. Run it again per project.

`init --check` finishes by calling the tracker once, so a wrong token or a
mistyped site fails now rather than on your first `prep`. Every answer can also
be given as an option (`--profile`, `--base-url`, `--email`, `--api-token`,
`--query`, `--repository`, `--account-note`); with `--yes` it asks nothing,
which is the form to use from a script.

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

## Browsing the queue

`lazyfish list` shows what the current profile's query matches, and changes
nothing: no worktree, no branch, no database row, not even a database file if
you have not run anything yet. Run it as often as you like.

```sh
$ lazyfish list
  PROFILE           work
                    company seat
  QUERY             project = PROJ AND assignee = currentUser()

    KEY         PRI     STATUS        SUMMARY
    PROJ-412    High    Sprint Ready  Password reset links expire too early
  * PROJ-388    High    Sprint Ready  Cognito token refresh fails
    PROJ-401    Medium  Sprint Ready  CSV export encoding broken

*  already tracked locally: PROJ-388 (ready for plan)
3 tickets. Start on one with 'lazyfish prep --ticket <KEY>'.
```

The `*` is the part a browser tab cannot give you: those tickets already have a
worktree or a recorded plan on this machine.

Pick anything from the list - not just the first row:

```sh
lazyfish prep --ticket PROJ-401
```

`--ticket` fetches by key, so it works even for a ticket the query does not
match. `prep` records whether you took the query's first result
(`was_top_pick`), which is the data that eventually says whether the query needs
adjusting.

`--limit` defaults to 20 and accepts up to 100, which is one page from the
tracker; a larger number is refused rather than quietly trimmed. `--json` emits
an array of `{key, summary, priority, status, is_known, known_state}` and
nothing else, for scripting:

```sh
lazyfish list --json | jq -r '.[] | select(.is_known | not) | .key'
```

## A full run

```sh
$ lazyfish prep
Candidates (5):
 * 1. PROJ-412  [High]  Password reset email links expire too early
   2. PROJ-398  [Medium]  Add pagination to the audit log endpoint
   ...
Prepared PROJ-412: Password reset email links expire too early
  profile           work
  branch            lazyfish/PROJ-412
  worktree          ~/.local/share/lazyfish/worktrees/work/PROJ-412/architect
  context           .../PROJ-412/architect/CLAUDE.md
  design brief      .../PROJ-412/architect/.lazyfish/plan-prompt.md
  write plan to     .../PROJ-412/architect/.lazyfish/plan.json

Open the worktree, run the AI tool of your choice, then 'lazyfish show' and 'lazyfish accept'.
cd /home/you/.local/share/lazyfish/worktrees/work/PROJ-412/architect
```

The path is `<worktree_root>/<profile>/<KEY>/<stage>`. Both middle levels earn
their place: the profile, so two profiles working the same ticket key do not
land in one directory, and the stage, so a later stage's workspace can be built
without destroying the one you can still read.

`CLAUDE.md` in that worktree holds the ticket text, its comments, the
attachments that were small and text-like enough to inline, your conventions
file, and a clearly labelled block of mechanical search hits.

`prep --no-hints` skips the mechanical code search when the ticket does not need
it, or when the repository is large enough that the scan is the slow part.

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
| `lazyfish next` | Say what the next step is and where to take it. `--json`. |
| `lazyfish promote` | Check the plan against its contract and keep it, or reject it. `--json`. |
| `lazyfish show` | Print the current plan and highlight open questions. |
| `lazyfish accept` | Promote, then record your decision: `--as-is`, `--modified` or `--reject`. |
| `lazyfish abandon` | Drop the ticket in flight, delete its worktree and branch. |
| `lazyfish status` | Acceptance rates, rejections, escalations and cycle time, per profile. |

`--profile <name>` is available on all of them. One ticket per profile is in
flight at a time, from `prep` until you have made a decision about the plan.

`next` and `promote` both speak JSON so that a driver can advance one stage
without reading anything meant for a person. They are the only two commands a
driver needs — and `accept` is deliberately not one of them: approving a plan is
not something a driver can do.

### When a plan does not pass

A plan that fails its contract is rejected, not silently accepted. The rejection
is data rather than a paragraph: which rule failed, the evidence, which attempt
this was, and where the work goes back to. Rewrite the plan and run `promote`
again.

Retries are budgeted — three per stage and twelve per ticket, counted in agent
runs — so a ticket that cannot converge stops and asks for you instead of
consuming attempts indefinitely. `lazyfish status` reports both counts.

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

## Troubleshooting

**Every command suddenly returns 401.** Atlassian API tokens expire after at
most 365 days, and an expired token fails exactly like a wrong one. Before
re-checking your config, check the age of the token at *id.atlassian.com ->
Security -> API tokens*; if it is close to a year old, that is the answer. Issue
a new one and run `lazyfish init --force --profile <name>` to replace that
profile, or edit the `credentials` file directly.

**"No credentials for profile X", but you are sure you wrote them.** The two
files are joined by profile name and located together, so both halves are worth
checking:

```sh
lazyfish status                 # validates config, no network
lazyfish --config <path> status # the same question about an explicit location
```

`--config` and `$LAZYFISH_CONFIG` both name `config.toml`, and the credentials
file is always read from that file's directory. Nothing is searched for: not the
current directory, not a parent, not the repository.

**A command exits 7 with "is not a SQLite database".** Almost always
`LAZYFISH_DATA_DIR` pointing somewhere unintended rather than a damaged file.
Check the variable first; if the path is right, move the file aside and lazyfish
will create a new database. Only the recorded tickets are lost.

**"was written by a newer lazyfish".** The database carries a schema version,
and lazyfish will not write to a file a later build has already upgraded — a
build that does not know a column cannot maintain it. Upgrading this install is
the fix.

You only see this with **two machines sharing one data directory**, and then the
order matters: whichever upgrades first migrates the database, and the other is
turned away until it upgrades too. Two machines with their own databases — the
default, since it lives in `~/.local/share/lazyfish/` — each migrate their own
copy and never meet.

Migrations run by themselves, on the first command after an upgrade. They are
idempotent, they only translate values lazyfish itself wrote, and tickets in
flight keep their worktrees, branches and baselines. Nothing is asked of you.

**"Branch lazyfish/KEY is already checked out by profile X".** Two profiles
matched the same ticket in the same repository. Worktrees are per profile,
branches are not. Give one profile its own `branch_prefix`, or finish the ticket
in the other profile first.

**`git worktree` complains about a path that already exists.** lazyfish adopts
its own worktrees and refuses to touch anything else in that directory. Move the
foreign directory aside, or point `worktree_root` elsewhere.

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
