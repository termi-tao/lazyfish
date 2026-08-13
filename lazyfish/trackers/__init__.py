"""Issue tracker clients.

`base` defines the data the rest of lazyfish is allowed to know about; each
other module is one implementation. Adding a tracker means adding a file here
and one line in `build_client`, never touching workspace or cli.
"""

from __future__ import annotations

from ..config import Credentials, Profile
from ..errors import ConfigError
from .base import Attachment, Comment, Ticket, TrackerClient

__all__ = ["Attachment", "Comment", "Ticket", "TrackerClient", "build_client"]


def build_client(profile: Profile, credentials: Credentials) -> TrackerClient:
    """Return the client implementation named by the profile's `tracker` key.

    This function is the only place that knows both a profile and a tracker
    implementation. Everything below it receives plain values.
    """
    if profile.tracker == "jira-cloud":
        from .jira_cloud import JiraCloudClient

        return JiraCloudClient(
            base_url=profile.base_url,
            email=credentials.email,
            api_token=credentials.api_token,
            query=profile.query,
            timeout_seconds=profile.timeout_seconds,
        )
    raise ConfigError(f"No tracker implementation for {profile.tracker!r}.")
