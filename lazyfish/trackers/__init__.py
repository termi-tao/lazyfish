"""Issue tracker clients.

`base` defines the data the rest of lazyfish is allowed to know about; each
other module is one implementation. Adding a tracker means adding a file here
and one line in `build_client`, never touching workspace or cli.
"""

from __future__ import annotations

from ..config import TrackerConfig
from ..errors import ConfigError
from .base import Attachment, Comment, Ticket, TrackerClient

__all__ = ["Attachment", "Comment", "Ticket", "TrackerClient", "build_client"]


def build_client(config: TrackerConfig) -> TrackerClient:
    """Return the client implementation named by [tracker] kind."""
    if config.kind == "jira-cloud":
        from .jira_cloud import JiraCloudClient

        return JiraCloudClient(config)
    raise ConfigError(f"No tracker implementation for kind {config.kind!r}.")
