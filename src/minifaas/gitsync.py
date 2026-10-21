"""Git access: detect the branch head cheaply, and fetch one commit shallowly.

Only HTTPS is supported. A token is passed through a GIT_ASKPASS helper that reads it
from a 0600 file, so it never appears in a command line, in `ps` output, or in a log.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from minifaas.config import Settings
from minifaas.errors import GitError

logger = logging.getLogger(__name__)

_ASKPASS_SCRIPT = "#!/bin/sh\ncat {path}\n"


def prepare_credentials(settings: Settings) -> None:
    """Write (or remove) the token and askpass helper for this configuration."""
    if not settings.git_token:
        for path in (settings.token_file, settings.askpass_file):
            path.unlink(missing_ok=True)
        return

    settings.token_file.write_text(settings.git_token)
    settings.token_file.chmod(0o600)
    settings.askpass_file.write_text(_ASKPASS_SCRIPT.format(path=settings.token_file))
    settings.askpass_file.chmod(0o700)


def authenticated_url(settings: Settings) -> str:
    """Put the (non-secret) username in the URL; the token comes from the askpass helper."""
    if not settings.git_token:
        return settings.git_url

    parts = urlsplit(settings.git_url)
    if parts.scheme != "https" or "@" in parts.netloc:
        return settings.git_url
    return urlunsplit(parts._replace(netloc=f"{settings.git_username}@{parts.netloc}"))


def git_env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if settings.git_token:
        env["GIT_ASKPASS"] = str(settings.askpass_file)
    else:
        env.pop("GIT_ASKPASS", None)
    return env


async def _run(args: list[str], env: dict[str, str], cwd: Path | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=env,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
        raise GitError(f"`{' '.join(args[:2])}` failed with exit {proc.returncode}: {detail}")
    return stdout.decode(errors="replace")


async def remote_head(settings: Settings) -> str:
    """Return the sha the tracked branch currently points at.

    `ls-remote` transfers no objects, which is what makes polling on a short interval
    essentially free.
    """
    url = authenticated_url(settings)
    ref = f"refs/heads/{settings.git_branch}"
    output = await _run(["git", "ls-remote", url, ref], git_env(settings))

    for line in output.splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref:
            return sha.strip()
    raise GitError(f"Branch {settings.git_branch!r} does not exist in {settings.git_url}.")


async def shallow_clone(settings: Settings, dest: Path) -> str:
    """Clone the tip of the tracked branch into ``dest`` and return the sha actually cloned.

    The sha is re-read from the checkout rather than trusted from the earlier ls-remote,
    because the branch can move between the two calls.
    """
    url = authenticated_url(settings)
    env = git_env(settings)
    await _run(
        ["git", "clone", "--depth", "1", "--branch", settings.git_branch, url, str(dest)],
        env,
    )
    return (await _run(["git", "-C", str(dest), "rev-parse", "HEAD"], env)).strip()


async def commit_info(settings: Settings, checkout: Path) -> dict[str, str]:
    """Subject, author and date of the checked-out commit, for the dashboard."""
    output = await _run(["git", "-C", str(checkout), "log", "-1", "--format=%s%x00%an%x00%cI"], git_env(settings))
    subject, author, date = ([*output.strip().split("\0"), "", "", ""])[:3]
    return {"subject": subject, "author": author, "date": date}


def web_url(settings: Settings) -> str:
    """The repository's browsable URL, with no credentials, for links to commits."""
    parts = urlsplit(settings.git_url)
    if parts.scheme != "https":
        return ""
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path.removesuffix(".git"), "", ""))
