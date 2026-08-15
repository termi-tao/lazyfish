# Contributing to lazyfish

Thanks for looking. This file covers the three things that are not obvious from
reading the code.

## Everything is written in English

All source, comments, docstrings, help text, error messages, log output,
templates, schema descriptions, test names, commit messages and branch names are
in English. CI rejects non-Latin codepoints in `lazyfish/` and in
`lazyfish/templates/`.

This is a hard requirement, not a style preference: the project is publicly
distributed and takes external contributions, and a non-English error message is
unactionable for anyone outside the team that wrote it.

**The inverse rule matters just as much.** Content that comes from a user's
tracker or repository is passed through character for character:

- Ticket titles, descriptions, comments and attachment filenames are never
  translated, normalised, transliterated or summarised. A team working in
  Chinese, Japanese or German must get their own text back unchanged in both
  `ticket.json` and `CLAUDE.md`.
- A repository's conventions file is injected as written.
- Values inside `plan.json` are not language-checked. The design brief asks for
  English prose as a default and says the team may override it; the keys are
  English either way, and that is all validation cares about.

Test fixtures under `tests/` are exempt from the CI check: they carry non-ASCII
ticket content on purpose, and `tests/test_i18n_passthrough.py` is what proves
the passthrough rule holds.

## The package never calls a model

`lazyfish/` must not import an LLM SDK (`anthropic`, `openai`,
`google.generativeai`, and friends) and must not start an AI CLI as a
subprocess. `tests/test_invariants.py` enforces both, and it runs on every PR.

This is what makes the tool's position on AI accounts a structural fact rather
than a promise: a tool that starts no AI process cannot select an account.

If you think a feature needs a model call, it belongs in a different tool that
consumes lazyfish's output.

## What `LF-2 D7` in a comment means

Comments and docstrings cite decisions as `LF-<n> D<m>` — ticket number, then
the numbered decision inside that ticket's design document. These are this
project's own design notes, not links into a public tracker, and they are not
distributed with the package. You will not find them in a clone, and you do not
need them to read the code: the comment always states the reasoning, and the
reference only says where it was first argued.

## Development setup

```sh
git clone https://github.com/termi-tao/lazyfish
cd lazyfish
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                      # the whole suite, no network, no real tracker
ruff check lazyfish tests   # lint
ruff format --check .       # formatting
```

The suite creates real git repositories and real worktrees in temporary
directories, and stubs the tracker with `httpx.MockTransport`. It never touches
your own configuration, credentials or database: everything is redirected
through the `LAZYFISH_CONFIG` and `LAZYFISH_DATA_DIR` environment variables.

Two tests deliberately use `XDG_CONFIG_HOME` instead, because an explicit
override cannot prove that the tool does not search the current directory - see
`tests/test_profiles.py`.

## Adding a tracker

1. Add `lazyfish/trackers/<name>.py` implementing the `TrackerClient` protocol
   from `trackers/base.py`.
2. Add the kind to `TRACKER_KINDS` in `config.py` and one branch in
   `trackers/__init__.py:build_client`.
3. Add tests using `httpx.MockTransport` with responses captured from a real
   instance, with anything identifying removed.

Name the module after the deployment, not just the product: Jira Server and Jira
Cloud differ in endpoints and authentication and are separate implementations.

Nothing outside `trackers/` may know which tracker is in use. If you find
yourself needing a tracker-specific field elsewhere, that field belongs on the
`Ticket` dataclass.

## Pull requests

- One change per PR, with a test that fails without it.
- Do not commit `config.toml`, `credentials`, `*.db`, or anything under an
  `artifacts/` directory. Ticket text and attachments live in the last one, and
  an API token lives in the second.
- Error messages are part of the interface. If you add a failure path, say which
  item is wrong and what was expected; "invalid configuration" is not enough.
