"""Configuration loading and validation.

This is the only place a first-time user is likely to get stuck, so validation
is strict and every message names the offending key and what was expected.

Two rules shape the design:

* Secrets never live in the config file. The file stores the *names* of
  environment variables; the values are read from the environment at call time.
  A credential pasted into config.toml is a hard startup failure, not a warning.
* Unknown keys are rejected. A silently ignored typo in a key name is far more
  expensive to debug than an error at load time.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError
from .paths import config_path, default_worktree_root

TRACKER_KINDS = ("jira-cloud",)
"""Accepted values for [tracker] kind. Written as an enum on purpose: adding
Jira Server or Linear later must not require rewriting the validation."""

DEFAULT_ATTACHMENT_MAX_BYTES = 100_000

DEFAULT_CONVENTIONS_PATH = ".lazyfish/conventions.md"
"""Used when a repo profile does not mention conventions at all. Setting the key
to an empty string turns the section off instead."""

DEFAULT_EMAIL_ENV = "LAZYFISH_EMAIL"
DEFAULT_TOKEN_ENV = "LAZYFISH_TOKEN"
"""Names of the environment variables holding the tracker credentials.

Both are optional in the config file. Naming your own environment variables is a
degree of freedom almost nobody wants, and asking for it costs two questions in
the setup wizard, so `init` does not ask and does not write the keys. They stay
readable from the file for the case that does need them: two config files against
two tracker instances, each with its own credentials."""

DEFAULT_ATTACHMENT_MIME_ALLOWLIST = (
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "application/xml",
    "text/xml",
    "application/x-yaml",
    "text/yaml",
)
"""Allowlist, not a blocklist (R1). Anything not listed here is recorded as
metadata only and never written to disk."""

ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

CREDENTIAL_PREFIXES = (
    "ATATT",
    "ATCTT",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "sk-",
    "xoxb-",
    "xoxp-",
    "glpat-",
    "lin_api_",
)

_CREDENTIAL_SCAN_EXEMPT_KEYS = frozenset(
    {
        "kind",
        "base_url",
        "query",
        "path",
        "conventions",
        "account_note",
        "search_globs",
        "worktree_root",
        "base_branch",
        "branch_prefix",
    }
)

_TRACKER_KEYS = frozenset(
    {
        "kind",
        "base_url",
        "email_env",
        "token_env",
        "query",
        "timeout_seconds",
        "attachment_max_bytes",
        "attachment_mime_allowlist",
    }
)

_REPO_KEYS = frozenset(
    {
        "path",
        "conventions",
        "search_globs",
        "account_note",
        "base_branch",
        "branch_prefix",
        "worktree_root",
    }
)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TrackerConfig:
    """Everything needed to talk to one issue tracker instance."""

    kind: str
    base_url: str
    query: str
    email_env: str = DEFAULT_EMAIL_ENV
    token_env: str = DEFAULT_TOKEN_ENV
    timeout_seconds: float = 30.0
    attachment_max_bytes: int = DEFAULT_ATTACHMENT_MAX_BYTES
    attachment_mime_allowlist: tuple[str, ...] = DEFAULT_ATTACHMENT_MIME_ALLOWLIST

    def credentials(self) -> tuple[str, str]:
        """Read (email, token) from the environment.

        Raises ConfigError naming the variable if either is unset, because
        "401 Unauthorized" is a much worse first-run experience.
        """
        email = os.environ.get(self.email_env, "").strip()
        token = os.environ.get(self.token_env, "").strip()
        missing = [
            name for name, value in ((self.email_env, email), (self.token_env, token)) if not value
        ]
        if missing:
            names = ", ".join(missing)
            raise ConfigError(
                f"Environment variable(s) not set: {names}\n"
                f"config.toml refers to them under [tracker], so lazyfish expects the "
                f"values in your environment. For example:\n"
                f"    export {missing[0]}='...'\n"
                f"Add the export to your shell profile to make it permanent."
            )
        return email, token


@dataclass(frozen=True)
class RepoProfile:
    """One target repository, selected on the command line with --repo."""

    name: str
    path: Path
    conventions: str | None = None
    search_globs: tuple[str, ...] = ()
    account_note: str | None = None
    base_branch: str | None = None
    branch_prefix: str = "lazyfish/"
    worktree_root: Path = field(default_factory=default_worktree_root)

    def conventions_path(self) -> Path | None:
        """Resolve the conventions file, which may be repo-relative or absolute.

        Deliberately not required to live inside the repo: some users cannot or
        will not commit a file to the target repository (see AC19). The return
        value is a location, not a promise that the file exists.
        """
        if not self.conventions:
            return None
        candidate = Path(self.conventions).expanduser()
        if candidate.is_absolute():
            return candidate
        return self.path / candidate

    def worktree_path(self, ticket_key: str) -> Path:
        return self.worktree_root / self.name / ticket_key

    def branch_name(self, ticket_key: str) -> str:
        return f"{self.branch_prefix}{ticket_key}"


@dataclass(frozen=True)
class Config:
    """The whole validated configuration file."""

    tracker: TrackerConfig
    repos: dict[str, RepoProfile]
    source: Path

    def repo(self, name: str | None) -> RepoProfile:
        """Look up a repo profile, defaulting to 'default'."""
        wanted = name or "default"
        try:
            return self.repos[wanted]
        except KeyError:
            known = ", ".join(sorted(self.repos)) or "(none)"
            hint = (
                f"Add a [repo.{wanted}] section to {self.source}."
                if name
                else f"Add a [repo.default] section to {self.source}, or pass --repo."
            )
            raise ConfigError(
                f"Unknown repo profile '{wanted}'. Defined profiles: {known}\n{hint}"
            ) from None


# --------------------------------------------------------------------------- #
# Credential detection (Q6)
# --------------------------------------------------------------------------- #


def _shannon_entropy(value: str) -> float:
    """Bits of entropy per character. Used only as one half of a heuristic."""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_like_credential(key: str, value: str) -> str | None:
    """Return a reason string if `value` looks like a pasted secret.

    Prefix matches apply to every key, including ones otherwise exempt: a token
    pasted into `token_env` is exactly the mistake this guard exists to catch.
    The entropy branch is skipped for keys whose values are legitimately long
    and human-written (URLs, JQL, paths), which would otherwise trip it.
    """
    for prefix in CREDENTIAL_PREFIXES:
        if value.startswith(prefix):
            return f"it starts with '{prefix}', a known API-token prefix"

    if key in _CREDENTIAL_SCAN_EXEMPT_KEYS:
        return None
    if len(value) < 20 or any(char.isspace() for char in value):
        return None

    classes = sum(
        (
            any(char.islower() for char in value),
            any(char.isupper() for char in value),
            any(char.isdigit() for char in value),
            any(not char.isalnum() for char in value),
        )
    )
    if classes >= 3 and _shannon_entropy(value) >= 3.5:
        return "it is a long, high-entropy string"
    return None


def _scan_for_credentials(data: Mapping[str, object], source: Path) -> None:
    """Walk the parsed document and refuse to start if a secret is present."""

    def walk(node: object, trail: tuple[str, ...]) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                walk(value, (*trail, str(key)))
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, (*trail, f"[{index}]"))
        elif isinstance(node, str) and trail:
            reason = _looks_like_credential(trail[-1], node)
            if reason is not None:
                location = ".".join(trail)
                raise ConfigError(
                    f"Possible credential found in {source} at '{location}': {reason}.\n"
                    f"lazyfish never reads secrets from the config file. Store the value "
                    f"in an environment variable and put the variable NAME in the config:\n"
                    f"    export LAZYFISH_TOKEN='<the value>'\n"
                    f'    token_env = "LAZYFISH_TOKEN"\n'
                    f"If this string is not a credential, rename the key or shorten the "
                    f"value; lazyfish errs on the side of refusing to run."
                )

    walk(data, ())


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _reject_unknown(section: str, table: Mapping[str, object], allowed: Iterable[str]) -> None:
    allowed_set = set(allowed)
    unknown = sorted(set(table) - allowed_set)
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in [{section}]: {', '.join(unknown)}\n"
            f"Accepted keys: {', '.join(sorted(allowed_set))}"
        )


def _require_str(section: str, table: Mapping[str, object], key: str) -> str:
    if key not in table:
        raise ConfigError(f"Missing required key '{key}' in [{section}] section.")
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{section}] {key} must be a non-empty string, got {value!r}.")
    return value.strip()


def _optional_str(section: str, table: Mapping[str, object], key: str) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"[{section}] {key} must be a string, got {value!r}.")
    return value


def _optional_str_list(section: str, table: Mapping[str, object], key: str) -> tuple[str, ...]:
    if key not in table:
        return ()
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(
            f'[{section}] {key} must be a list of strings, for example {key} = ["*.py", "*.ts"]'
        )
    return tuple(value)


def _env_name(section: str, table: Mapping[str, object], key: str, default: str) -> str:
    """Read an environment variable NAME, falling back to the default.

    Absent is normal: `init` does not write these keys. Present but malformed is
    worth a loud error, since a value where a name belongs is the mistake this
    check exists for.
    """
    if key not in table:
        return default
    value = _require_str(section, table, key)
    if not ENV_NAME_RE.match(value):
        raise ConfigError(
            f"[{section}] {key} must be the NAME of an environment variable "
            f"(upper case letters, digits and underscores), got {value!r}.\n"
            f"lazyfish reads the value from the environment; it is never stored "
            f"in the config file."
        )
    return value


def _parse_tracker(table: Mapping[str, object]) -> TrackerConfig:
    _reject_unknown("tracker", table, _TRACKER_KEYS)

    kind = _require_str("tracker", table, "kind")
    if kind not in TRACKER_KINDS:
        raise ConfigError(
            f"[tracker] kind must be one of: {', '.join(TRACKER_KINDS)}; got {kind!r}."
        )

    base_url = _require_str("tracker", table, "base_url").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"[tracker] base_url must start with http:// or https://, got {base_url!r}.\n"
            f'Example: base_url = "https://your-org.atlassian.net"'
        )

    timeout = table.get("timeout_seconds", 30.0)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(f"[tracker] timeout_seconds must be a positive number, got {timeout!r}.")

    max_bytes = table.get("attachment_max_bytes", DEFAULT_ATTACHMENT_MAX_BYTES)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ConfigError(
            f"[tracker] attachment_max_bytes must be a non-negative integer, got {max_bytes!r}."
        )

    allowlist = _optional_str_list("tracker", table, "attachment_mime_allowlist")

    return TrackerConfig(
        kind=kind,
        base_url=base_url,
        email_env=_env_name("tracker", table, "email_env", DEFAULT_EMAIL_ENV),
        token_env=_env_name("tracker", table, "token_env", DEFAULT_TOKEN_ENV),
        query=_require_str("tracker", table, "query"),
        timeout_seconds=float(timeout),
        attachment_max_bytes=max_bytes,
        attachment_mime_allowlist=allowlist or DEFAULT_ATTACHMENT_MIME_ALLOWLIST,
    )


def _parse_repo(name: str, table: Mapping[str, object]) -> RepoProfile:
    section = f"repo.{name}"
    _reject_unknown(section, table, _REPO_KEYS)

    raw_path = _require_str(section, table, "path")
    path = Path(raw_path).expanduser()
    if not path.exists():
        raise ConfigError(
            f"[{section}] path does not exist: {path}\nIt must point at a git repository checkout."
        )
    if not path.is_dir():
        raise ConfigError(f"[{section}] path is not a directory: {path}")
    if not (path / ".git").exists():
        raise ConfigError(f"[{section}] path is not a git repository (no .git found): {path}")

    branch_prefix = _optional_str(section, table, "branch_prefix") or "lazyfish/"
    raw_worktree_root = _optional_str(section, table, "worktree_root")
    worktree_root = (
        Path(raw_worktree_root).expanduser() if raw_worktree_root else default_worktree_root()
    )

    # An absent key means "use the default location"; an empty string means the
    # user deliberately turned the conventions section off.
    if "conventions" in table:
        conventions = _optional_str(section, table, "conventions") or None
    else:
        conventions = DEFAULT_CONVENTIONS_PATH

    return RepoProfile(
        name=name,
        path=path.resolve(),
        conventions=conventions,
        search_globs=_optional_str_list(section, table, "search_globs"),
        account_note=_optional_str(section, table, "account_note"),
        base_branch=_optional_str(section, table, "base_branch"),
        branch_prefix=branch_prefix,
        worktree_root=worktree_root,
    )


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def parse_config(data: Mapping[str, object], source: Path) -> Config:
    """Validate an already-parsed TOML document."""
    _scan_for_credentials(data, source)

    _reject_unknown("<top level>", data, {"tracker", "repo"})

    tracker_table = data.get("tracker")
    if not isinstance(tracker_table, Mapping):
        raise ConfigError(
            f"Missing [tracker] section in {source}.\n"
            f"Run 'lazyfish init' to generate a working configuration."
        )

    repo_table = data.get("repo")
    if not isinstance(repo_table, Mapping) or not repo_table:
        raise ConfigError(
            f"No repo profiles defined in {source}.\n"
            f"Add at least one, for example:\n"
            f"    [repo.default]\n"
            f'    path = "~/dev/your-repo"'
        )

    repos: dict[str, RepoProfile] = {}
    for name, table in repo_table.items():
        if not isinstance(table, Mapping):
            raise ConfigError(
                f"[repo.{name}] must be a table, for example:\n"
                f"    [repo.{name}]\n"
                f'    path = "~/dev/your-repo"'
            )
        repos[str(name)] = _parse_repo(str(name), table)

    return Config(tracker=_parse_tracker(tracker_table), repos=repos, source=source)


def load_config(path: Path | None = None, *, require_credentials: bool = True) -> Config:
    """Read, parse and validate config.toml."""
    source = path or config_path()
    if not source.exists():
        raise ConfigError(f"No configuration file at {source}\nRun 'lazyfish init' to create one.")
    try:
        with open(source, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {source}: {exc}") from exc

    config = parse_config(data, source)
    if require_credentials:
        config.tracker.credentials()
    return config
