"""Error types.

Every error raised on a user-reachable path derives from LazyfishError. The CLI
catches that base class and prints `message` without a traceback, so the message
itself has to be actionable: say which item is wrong and what was expected.
"""


class LazyfishError(Exception):
    """Base class for all errors intended to reach the user as plain text."""

    exit_code = 1


class ConfigError(LazyfishError):
    """Configuration file is missing, malformed, or contains a credential."""

    exit_code = 2


class TrackerError(LazyfishError):
    """The issue tracker rejected a request or returned an unexpected shape."""

    exit_code = 3


class WorkspaceError(LazyfishError):
    """A git or filesystem operation on the target repository failed."""

    exit_code = 4


class StateError(LazyfishError):
    """The requested state transition is not legal for the current record."""

    exit_code = 5


class ValidationError(LazyfishError):
    """plan.json failed schema validation or one of the extra rules."""

    exit_code = 6


class DatabaseError(LazyfishError):
    """The local database could not be opened, read or written.

    Distinct from StateError, which is about a transition the records forbid.
    This one is about the file itself: missing, not a database, locked, or on a
    directory that cannot be written to.
    """

    exit_code = 7
