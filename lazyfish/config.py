"""Configuration loading and validation.

Two files, both in ~/.config/lazyfish/ and nowhere else:

* config.toml   - non-sensitive. Safe to commit to dotfiles, paste into a bug
                  report, or hand to a colleague to copy.
* credentials   - sensitive, mode 600, one section per profile.

The split exists for that first line. A config file with no secrets in it can be
shared, and sharing is what gets a public tool adopted. Once a secret can live in
it, none of that is true any more, and the credential scan below loses its
meaning too: it could no longer distinguish "this does not belong here" from
"this is exactly where it belongs".

Shape of config.toml:

    default_profile = "work"

    [defaults]          any non-identity key, inherited by every profile
    ...

    [profile.work]      identity keys live here and only here
    tracker  = "jira-cloud"
    base_url = "https://your-org.atlassian.net"
    query    = "..."
    repo     = "~/dev/your-repo"

Identity keys are rejected in [defaults] on purpose. "Every profile shares one
query" is precisely the shape this structure exists to make impossible: a query
and a repository that come from different places let you pull a ticket from
project A and build its worktree in project B.
"""

from __future__ import annotations

import math
import os
import stat
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .paths import config_path, credentials_path, default_worktree_root

TRACKER_KINDS = ("jira-cloud",)
"""Accepted values for the `tracker` key. Written as an enum on purpose: adding
Jira Server or Linear later must not require rewriting the validation."""

EMAIL_ENV = "LAZYFISH_EMAIL"
TOKEN_ENV = "LAZYFISH_TOKEN"
PROFILE_ENV = "LAZYFISH_PROFILE"

DEFAULT_CONVENTIONS_PATH = ".lazyfish/conventions.md"
DEFAULT_BRANCH_PREFIX = "lazyfish/"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_ATTACHMENT_MAX_BYTES = 100_000

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

IDENTITY_KEYS = ("tracker", "base_url", "query", "repo")
"""Keys that bind a profile to one project. They may not be defaulted."""

OVERRIDABLE_KEYS = (
    "conventions",
    "search_globs",
    "account_note",
    "base_branch",
    "branch_prefix",
    "worktree_root",
    "timeout_seconds",
    "attachment_max_bytes",
    "attachment_mime_allowlist",
)
"""Everything else: settable in [defaults], overridable per profile."""

PROFILE_KEYS = frozenset(IDENTITY_KEYS + OVERRIDABLE_KEYS)
TOP_LEVEL_KEYS = frozenset({"default_profile", "defaults", "profile"})

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

_CREDENTIAL_SCAN_EXEMPT_KEYS = frozenset(PROFILE_KEYS | {"default_profile"})
"""Keys whose values are legitimately long and human-written. The prefix check
still applies to them; only the entropy heuristic is skipped."""


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Profile:
    """One project: a tracker query and the repository it belongs to.

    The two arrive together or not at all. Nothing in lazyfish can assemble a
    query from one profile and a repository from another.
    """

    name: str
    tracker: str
    base_url: str
    query: str
    repo: Path
    conventions: str | None = DEFAULT_CONVENTIONS_PATH
    search_globs: tuple[str, ...] = ()
    account_note: str | None = None
    base_branch: str | None = None
    branch_prefix: str = DEFAULT_BRANCH_PREFIX
    worktree_root: Path | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    attachment_max_bytes: int = DEFAULT_ATTACHMENT_MAX_BYTES
    attachment_mime_allowlist: tuple[str, ...] = DEFAULT_ATTACHMENT_MIME_ALLOWLIST

    def conventions_path(self) -> Path | None:
        """Resolve the conventions file, or None when the section is switched off.

        Three states, see D2: key absent means the default location, an empty
        string means "no conventions section", any other value is the path,
        relative to the repository root or absolute.
        """
        if not self.conventions:
            return None
        candidate = Path(self.conventions).expanduser()
        if candidate.is_absolute():
            return candidate
        return self.repo / candidate

    def require_repo(self) -> Path:
        """The repository path, checked to be a usable git checkout.

        Called by the commands that touch the working tree - anything that
        creates or removes a worktree - so they fail with a readable message
        instead of a git error (LF-1 AC13).
        """
        if not self.repo.exists():
            raise ConfigError(
                f"[profile.{self.name}] repo does not exist: {self.repo}\n"
                f"It must point at a git repository checkout on this machine."
            )
        if not self.repo.is_dir():
            raise ConfigError(f"[profile.{self.name}] repo is not a directory: {self.repo}")
        if not (self.repo / ".git").exists():
            raise ConfigError(
                f"[profile.{self.name}] repo is not a git repository (no .git found): {self.repo}"
            )
        return self.repo

    def worktree_root_path(self) -> Path:
        return self.worktree_root or default_worktree_root()

    def worktree_path(self, ticket_key: str) -> Path:
        return self.worktree_root_path() / self.name / ticket_key

    def branch_name(self, ticket_key: str) -> str:
        return f"{self.branch_prefix}{ticket_key}"


@dataclass(frozen=True)
class Credentials:
    """Tracker credentials for one profile."""

    email: str
    api_token: str


@dataclass(frozen=True)
class Config:
    """The validated contents of config.toml."""

    profiles: dict[str, Profile]
    default_profile: str | None
    source: Path

    def select(self, explicit: str | None = None) -> Profile:
        """Pick a profile: --profile, then LAZYFISH_PROFILE, then default_profile."""
        wanted = explicit or os.environ.get(PROFILE_ENV, "").strip() or None
        origin = (
            "--profile"
            if explicit
            else (f"${PROFILE_ENV}" if os.environ.get(PROFILE_ENV, "").strip() else None)
        )

        if wanted is None:
            wanted = self.default_profile
            origin = "default_profile"
        if wanted is None:
            if len(self.profiles) == 1:
                return next(iter(self.profiles.values()))
            raise ConfigError(
                f"No profile selected and {self.source} has no default_profile.\n"
                f'Add default_profile = "<name>" at the top of the file, or pass '
                f"--profile <name>.\nDefined profiles: {self._known()}"
            )
        try:
            return self.profiles[wanted]
        except KeyError:
            raise ConfigError(
                f"Unknown profile '{wanted}' (selected by {origin}).\n"
                f"Defined profiles: {self._known()}\n"
                f"Profiles are defined as [profile.<name>] in {self.source}."
            ) from None

    def _known(self) -> str:
        return ", ".join(sorted(self.profiles)) or "(none)"


# --------------------------------------------------------------------------- #
# Credential detection in config.toml (never in credentials)
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
    """Return a reason string if `value` looks like a pasted secret."""
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
    """Refuse to start if config.toml contains something that looks like a secret.

    Only config.toml is scanned. A credential in the credentials file is the
    expected state of the world, not a finding.
    """

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
                    f"Credentials belong in {credentials_path()}, which is never "
                    f"shared and is kept at mode 600:\n"
                    f"    [<profile name>]\n"
                    f'    email     = "you@example.com"\n'
                    f'    api_token = "<the value>"\n'
                    f"Delete the line from {source.name} and put it there instead.\n"
                    f"If this string is not a credential, rename the key or shorten "
                    f"the value; lazyfish errs on the side of refusing to run."
                )

    walk(data, ())


# --------------------------------------------------------------------------- #
# Small validators
# --------------------------------------------------------------------------- #


def _reject_unknown(section: str, table: Mapping[str, object], allowed: Iterable[str]) -> None:
    allowed_set = set(allowed)
    unknown = sorted(set(table) - allowed_set)
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in [{section}]: {', '.join(unknown)}\n"
            f"Accepted keys: {', '.join(sorted(allowed_set))}"
        )


def _as_str(section: str, table: Mapping[str, object], key: str) -> str:
    value = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"[{section}] {key} must be a string, got {value!r}.")
    return value


def _as_non_empty_str(section: str, table: Mapping[str, object], key: str) -> str:
    value = _as_str(section, table, key)
    if not value.strip():
        raise ConfigError(f"[{section}] {key} must not be empty.")
    return value.strip()


def _as_optional_str(section: str, table: Mapping[str, object], key: str) -> str | None:
    """Empty string is a value, not an absence. See D2."""
    value = _as_str(section, table, key)
    return value or None


def _as_str_list(section: str, table: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(
            f'[{section}] {key} must be a list of strings, for example {key} = ["*.py"]'
        )
    return tuple(value)


def _as_positive_number(section: str, table: Mapping[str, object], key: str) -> float:
    value = table[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"[{section}] {key} must be a positive number, got {value!r}.")
    return float(value)


def _as_non_negative_int(section: str, table: Mapping[str, object], key: str) -> int:
    value = table[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"[{section}] {key} must be a non-negative integer, got {value!r}.")
    return value


def _as_path(section: str, table: Mapping[str, object], key: str) -> Path:
    return Path(_as_non_empty_str(section, table, key)).expanduser()


_PARSERS: dict[str, Callable[[str, Mapping[str, object], str], Any]] = {
    "conventions": _as_optional_str,
    "search_globs": _as_str_list,
    "account_note": _as_optional_str,
    "base_branch": _as_optional_str,
    "branch_prefix": _as_str,
    "worktree_root": _as_path,
    "timeout_seconds": _as_positive_number,
    "attachment_max_bytes": _as_non_negative_int,
    "attachment_mime_allowlist": _as_str_list,
}

_FALLBACKS: dict[str, Any] = {
    "conventions": DEFAULT_CONVENTIONS_PATH,
    "search_globs": (),
    "account_note": None,
    "base_branch": None,
    "branch_prefix": DEFAULT_BRANCH_PREFIX,
    "worktree_root": None,
    "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
    "attachment_max_bytes": DEFAULT_ATTACHMENT_MAX_BYTES,
    "attachment_mime_allowlist": DEFAULT_ATTACHMENT_MIME_ALLOWLIST,
}


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _resolve_overridable(
    key: str, name: str, profile: Mapping[str, object], defaults: Mapping[str, object]
) -> Any:
    """profile > [defaults] > built-in fallback.

    Presence of the key decides, never truthiness. `conventions = ""` means the
    section is switched off and must not fall back to the default path; an empty
    search_globs list means "no glob filter" and must not fall back either (R2).
    """
    parser = _PARSERS[key]
    if key in profile:
        return parser(f"profile.{name}", profile, key)
    if key in defaults:
        return parser("defaults", defaults, key)
    return _FALLBACKS[key]


def _parse_defaults(table: Mapping[str, object]) -> Mapping[str, object]:
    misplaced = [key for key in IDENTITY_KEYS if key in table]
    if misplaced:
        raise ConfigError(
            f"[defaults] must not contain {', '.join(misplaced)}.\n"
            f"tracker, base_url, query and repo identify one project, so they "
            f"belong in a [profile.<name>] table and nowhere else. Sharing a query "
            f"across profiles is what lets a ticket from one project end up in "
            f"another project's worktree."
        )
    _reject_unknown("defaults", table, OVERRIDABLE_KEYS)
    return table


def _parse_profile(
    name: str, table: Mapping[str, object], defaults: Mapping[str, object]
) -> Profile:
    section = f"profile.{name}"
    _reject_unknown(section, table, PROFILE_KEYS)

    for key in IDENTITY_KEYS:
        if key not in table:
            raise ConfigError(
                f"Missing required key '{key}' in [{section}].\n"
                f"Every profile needs all of: {', '.join(IDENTITY_KEYS)}."
            )

    tracker = _as_non_empty_str(section, table, "tracker")
    if tracker not in TRACKER_KINDS:
        raise ConfigError(
            f"[{section}] tracker must be one of: {', '.join(TRACKER_KINDS)}; got {tracker!r}."
        )

    base_url = _as_non_empty_str(section, table, "base_url").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"[{section}] base_url must start with http:// or https://, "
            f"got {base_url!r}.\n"
            f'Example: base_url = "https://your-org.atlassian.net"'
        )

    # Whether the path exists is checked by the commands that need it, not
    # here. `lazyfish list` only talks to the tracker, and refusing to run it
    # because some other profile's checkout is on an unmounted disk would be
    # obstruction rather than validation (LF-3 AC9).
    repo = _as_path(section, table, "repo")

    resolved = {key: _resolve_overridable(key, name, table, defaults) for key in OVERRIDABLE_KEYS}
    return Profile(
        name=name,
        tracker=tracker,
        base_url=base_url,
        query=_as_non_empty_str(section, table, "query"),
        repo=repo.resolve(),
        **resolved,
    )


def parse_config(data: Mapping[str, object], source: Path) -> Config:
    """Validate an already-parsed config.toml document."""
    _scan_for_credentials(data, source)
    _reject_unknown("<top level>", data, TOP_LEVEL_KEYS)

    raw_defaults = data.get("defaults", {})
    if not isinstance(raw_defaults, Mapping):
        raise ConfigError(f"[defaults] must be a table in {source}.")
    defaults = _parse_defaults(raw_defaults)

    raw_profiles = data.get("profile")
    if not isinstance(raw_profiles, Mapping) or not raw_profiles:
        raise ConfigError(
            f"No profiles defined in {source}.\n"
            f"Add at least one, for example:\n"
            f"    [profile.work]\n"
            f'    tracker  = "jira-cloud"\n'
            f'    base_url = "https://your-org.atlassian.net"\n'
            f'    query    = "assignee = currentUser()"\n'
            f'    repo     = "~/dev/your-repo"\n'
            f"Or run 'lazyfish init'."
        )

    profiles: dict[str, Profile] = {}
    for name, table in raw_profiles.items():
        if not isinstance(table, Mapping):
            raise ConfigError(f"[profile.{name}] must be a table in {source}.")
        profiles[str(name)] = _parse_profile(str(name), table, defaults)

    default_profile = None
    if "default_profile" in data:
        default_profile = _as_non_empty_str("<top level>", data, "default_profile")
        if default_profile not in profiles:
            raise ConfigError(
                f"default_profile is {default_profile!r}, but no [profile."
                f"{default_profile}] is defined in {source}.\n"
                f"Defined profiles: {', '.join(sorted(profiles))}"
            )

    return Config(profiles=profiles, default_profile=default_profile, source=source)


def load_config(path: Path | None = None) -> Config:
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
    return parse_config(data, source)


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def _check_permissions(path: Path) -> None:
    """Refuse a credentials file that anyone but the owner can read.

    POSIX only. On Windows the mode bits do not describe access, so checking
    them would reject correct setups for no gain.
    """
    if os.name != "posix":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"{path} is readable by group or others (mode {mode:04o}).\n"
            f"It holds your API token. Fix it with:\n"
            f"    chmod 600 {path}"
        )


def _read_credentials_file(path: Path) -> Mapping[str, object]:
    if not path.exists():
        raise ConfigError(
            f"No credentials file at {path}\n"
            f"Run 'lazyfish init' to create one, or set {EMAIL_ENV} and "
            f"{TOKEN_ENV} in the environment."
        )
    _check_permissions(path)
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc


def resolve_credentials(profile_name: str, path: Path | None = None) -> Credentials:
    """Credentials for one profile: environment first, then the file.

    The file is opened only when a value is actually needed from it, which is
    what keeps the permission check and the CI path from contradicting each
    other (D6). With both variables exported, the file is never touched and may
    be absent, or wrongly permissioned, without consequence.
    """
    source = path or credentials_path()
    email = os.environ.get(EMAIL_ENV, "").strip()
    token = os.environ.get(TOKEN_ENV, "").strip()
    if email and token:
        return Credentials(email=email, api_token=token)

    document = _read_credentials_file(source)
    section = document.get(profile_name)
    if not isinstance(section, Mapping):
        known = ", ".join(sorted(str(key) for key in document)) or "(none)"
        raise ConfigError(
            f"No credentials for profile '{profile_name}' in {source}\n"
            f"Add a section named after the profile:\n"
            f"    [{profile_name}]\n"
            f'    email     = "you@example.com"\n'
            f'    api_token = "<your API token>"\n'
            f"Sections present: {known}"
        )

    if not email:
        email = _credential_field(section, "email", profile_name, source)
    if not token:
        token = _credential_field(section, "api_token", profile_name, source)
    return Credentials(email=email, api_token=token)


def _credential_field(
    section: Mapping[str, object], key: str, profile_name: str, source: Path
) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"[{profile_name}] in {source} has no usable '{key}'.\n"
            f"Expected a non-empty string. Jira Cloud needs both email and "
            f"api_token; the token comes from id.atlassian.com -> Security -> "
            f"API tokens."
        )
    return value.strip()
