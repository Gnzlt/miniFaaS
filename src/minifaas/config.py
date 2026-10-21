"""Every tunable, in one place: MINIFAAS_* environment variables first, then /data/config.json.

The config file is what the dashboard's setup wizard and Settings page write. An environment
variable always wins over it, so a compose file stays authoritative for whatever it sets.
No other module reads os.environ or config.json for configuration. Anything not listed here
is left untouched in the environment and reaches the functions as-is, together with the
variables set from the dashboard, which is how a function receives its own settings and secrets.
"""

from __future__ import annotations

import json
import os
import re
from functools import cached_property
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from minifaas import schedules

#: Never passed to a function. MINIFAAS_* settings are the runtime's own; the token is the deploy credential.
RESERVED_PREFIXES = ("MINIFAAS_", "_MINIFAAS_")

#: A variable name the dashboard accepts: what a shell would accept, minus the reserved prefixes.
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: What config.json and the dashboard may set. Host, ports and the data dir are env-only: they have to
#: match the container's port mapping and volume, and the data dir is where config.json itself lives.
EDITABLE = (
    "git_url",
    "git_branch",
    "git_token",
    "git_username",
    "source_root",
    "entry_module",
    "update_schedule",
    "timezone",
    "threads",
    "function_timeout",
    "misfire_grace",
    "retention_days",
    "keep_releases",
    "build_timeout",
    "max_restarts",
    "shutdown_grace",
    "log_level",
)


class SettingsError(ValueError):
    """Settings from the dashboard that can't be used; the message is shown in the form."""


class Settings(BaseSettings):
    """Runtime configuration for the supervisor."""

    model_config = SettingsConfigDict(env_prefix="MINIFAAS_", extra="ignore")

    # --- source ---
    git_url: str = ""
    git_branch: str = "main"
    git_token: str = ""
    git_username: str = "x-access-token"
    #: Folder holding the entry module. Empty means wherever the functions are found (see locate.py).
    source_root: str = ""
    #: Module to import, without .py. Empty means whichever file declares the functions.
    entry_module: str = ""

    # --- update behaviour ---
    #: When to check the branch for new commits, besides before every run. "off" checks only then.
    update_schedule: str = "0 6,18 * * *"
    keep_releases: int = Field(default=2, ge=1)
    max_restarts: int = Field(default=5, ge=1)
    shutdown_grace: int = Field(default=10, ge=1)
    build_timeout: int = Field(default=900, ge=1)

    # --- serving ---
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    dashboard_port: int = Field(default=8081, ge=1, le=65535)
    threads: int = Field(default=8, ge=1)
    timezone: str = "UTC"
    misfire_grace: int = Field(default=60, ge=1)
    #: Cloud Functions' default; a function's own timeout_sec wins.
    function_timeout: int = Field(default=540, ge=1)

    # --- history, paths and logging ---
    retention_days: int = Field(default=14, ge=1)
    data_dir: Path = Path("/data")
    log_level: str = "INFO"

    @property
    def configured(self) -> bool:
        return bool(self.git_url)

    @cached_property
    def config_file(self) -> Path:
        return self.data_dir / "config.json"

    @cached_property
    def releases_dir(self) -> Path:
        return self.data_dir / "releases"

    @cached_property
    def current_link(self) -> Path:
        """Symlink to the live release. Swapping it is the only way a deploy happens."""
        return self.data_dir / "current"

    @cached_property
    def uv_cache_dir(self) -> Path:
        """Shared across releases, so rebuilding a heavy venv is fast after the first time."""
        return self.data_dir / "uv-cache"

    @cached_property
    def state_file(self) -> Path:
        """The supervisor's record of what is deployed and which schedules are paused."""
        return self.data_dir / "state.json"

    @cached_property
    def env_file(self) -> Path:
        """Variables set from the dashboard, layered over the container's environment."""
        return self.data_dir / "env.json"

    @cached_property
    def db_file(self) -> Path:
        return self.data_dir / "minifaas.db"

    @cached_property
    def token_file(self) -> Path:
        return self.data_dir / ".git-token"

    @cached_property
    def askpass_file(self) -> Path:
        return self.data_dir / ".git-askpass"

    @staticmethod
    def venv_python(release_dir: Path) -> Path:
        return release_dir / ".venv" / "bin" / "python"

    @staticmethod
    def manifest_file(release_dir: Path) -> Path:
        """What discovery found in a release, written once at build time."""
        return release_dir / ".minifaas-manifest.json"


def env_name(field: str) -> str:
    return f"MINIFAAS_{field.upper()}"


def locked_fields() -> dict[str, str]:
    """Editable settings an environment variable already sets, by field → variable name."""
    return {field: env_name(field) for field in EDITABLE if env_name(field) in os.environ}


def read_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if k in EDITABLE} if isinstance(data, dict) else {}


def load_settings() -> Settings:
    """The environment, then config.json for whatever the environment leaves unset."""
    data_dir = Settings().data_dir
    locked = locked_fields()
    values = {k: v for k, v in read_config(data_dir / "config.json").items() if k not in locked}
    try:
        return Settings(**values)
    except ValidationError:
        # A hand-edited file with a bad value must not stop the container: fall back to the environment.
        return Settings()


def _validate(values: dict[str, Any]) -> Settings:
    try:
        settings = Settings(**values)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise SettingsError(problems) from exc
    url = settings.git_url
    if url and not url.startswith(("https://", "file://")):
        raise SettingsError("The repository URL must start with https:// (SSH isn't supported).")
    try:
        schedules.zone(settings.timezone)
        if not schedules.is_off(settings.update_schedule):
            schedules.trigger(settings.update_schedule, settings.timezone)
    except schedules.InvalidScheduleError as exc:
        raise SettingsError(str(exc)) from exc
    return settings


def save_config(data_dir: Path, changes: dict[str, Any], clear_token: bool = False) -> Settings:
    """Merge the dashboard's changes into config.json, refusing anything that wouldn't work.

    An empty token means "keep the saved one", since the dashboard never sees it.
    """
    path = data_dir / "config.json"
    current = read_config(path)
    merged = dict(current)
    for key, value in changes.items():
        if key not in EDITABLE:
            continue
        if key == "git_token" and not value:
            continue
        merged[key] = value.strip() if isinstance(value, str) else value
    if clear_token:
        merged.pop("git_token", None)
    # Empty fields fall back to their defaults rather than being stored as empty strings.
    merged = {k: v for k, v in merged.items() if v not in ("", None) or k == "source_root"}

    locked = locked_fields()
    settings = _validate({k: v for k, v in merged.items() if k not in locked} | {"data_dir": data_dir})
    # Store the parsed values ("60" from a form becomes 60), and only those that differ from the default,
    # so a better default in a later version still reaches an installation that never changed it.
    merged = {k: v if k in locked else getattr(settings, k) for k, v in merged.items()}
    merged = {k: v for k, v in merged.items() if k in locked or v != Settings.model_fields[k].default}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2))
    tmp.chmod(0o600)
    os.replace(tmp, path)
    return load_settings()
