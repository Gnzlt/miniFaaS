"""Exception types shared across the supervisor."""

from __future__ import annotations


class MiniFaaSError(Exception):
    """Base class for every error miniFaaS raises deliberately."""


class ConfigError(MiniFaaSError):
    """Configuration is missing or contradictory."""


class GitError(MiniFaaSError):
    """A git command failed."""


class ReleaseBuildError(MiniFaaSError):
    """A candidate commit could not be turned into a runnable release."""
