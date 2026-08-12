"""Filesystem locations.

Everything lazyfish reads or writes outside the target repository resolves
through this module. The LAZYFISH_CONFIG and LAZYFISH_DATA_DIR overrides exist
so a test (or a user with an unusual setup) can relocate state without
monkeypatching internals.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_FILENAME = "config.toml"
DB_FILENAME = "lazyfish.db"


def config_home() -> Path:
    """Directory holding config.toml."""
    override = os.environ.get("LAZYFISH_CONFIG")
    if override:
        return Path(override).expanduser().parent
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "lazyfish"


def config_path() -> Path:
    """Full path to config.toml."""
    override = os.environ.get("LAZYFISH_CONFIG")
    if override:
        return Path(override).expanduser()
    return config_home() / CONFIG_FILENAME


def data_home() -> Path:
    """Directory holding the SQLite database and default worktree root."""
    override = os.environ.get("LAZYFISH_DATA_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "lazyfish"


def db_path() -> Path:
    """Full path to the SQLite database file."""
    return data_home() / DB_FILENAME


def default_worktree_root() -> Path:
    """Where worktrees go when a repo profile does not say otherwise."""
    return data_home() / "worktrees"
