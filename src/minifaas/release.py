"""Turn a commit into a runnable release, or refuse it.

A release is a directory holding one checkout plus its own virtualenv. Building one is
all-or-nothing: clone, install, discovery and schedule checks all have to succeed before
the directory is given its final name. Nothing here touches the live release, so a broken
commit costs a failed deploy in the dashboard rather than an outage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import minifaas
from minifaas import schedules
from minifaas.config import RESERVED_PREFIXES, Settings
from minifaas.errors import ReleaseBuildError
from minifaas.gitsync import commit_info, shallow_clone
from minifaas.locate import Entry, find_entry, requirements_for

#: The directory that must be on PYTHONPATH for `python -m minifaas.worker` to work in a release venv.
PACKAGE_ROOT = Path(minifaas.__file__).resolve().parents[1]

#: The one thing every release needs besides its own requirements: the server the worker is built on.
#: Firebase's SDK already depends on it, so a Firebase project resolves it from its own pins.
RUNTIME_REQUIREMENT = "functions-framework>=3.5,<4"

BuildLog = Callable[[str], None]


@dataclass(frozen=True)
class Release:
    sha: str
    directory: Path
    manifest: dict[str, Any]

    @property
    def id(self) -> str:
        return self.directory.name


def release_id(settings: Settings, sha: str) -> str:
    """A release's directory name: the commit plus what was built from it.

    The same commit built with another source folder or entry module is a different
    release, so changing those on the Settings page never reuses a stale build.
    """
    inputs = "\0".join((settings.git_url, settings.source_root, settings.entry_module))
    return f"{sha}-{hashlib.sha1(inputs.encode()).hexdigest()[:8]}"


def function_environment(settings: Settings, source_dir: Path, overrides: dict[str, str]) -> dict[str, str]:
    """The environment a function runs in: the container's, then the dashboard's variables on top.

    MINIFAAS_* settings, the deploy token among them, never reach a function.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(RESERVED_PREFIXES)}
    env.pop("GIT_ASKPASS", None)
    env.update(overrides)
    env["PYTHONPATH"] = os.pathsep.join([str(PACKAGE_ROOT), str(source_dir)])
    env["PYTHONUNBUFFERED"] = "1"
    return env


def load_manifest(release_dir: Path) -> dict[str, Any]:
    try:
        return json.loads(Settings.manifest_file(release_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return {"functions": [], "unsupported": []}


def _uv() -> str:
    path = shutil.which("uv")
    if path is None:
        raise ReleaseBuildError("`uv` is not on PATH. miniFaaS installs dependencies with uv only.")
    return path


async def _run(args: list[str], env: dict[str, str], timeout: int, label: str, log: BuildLog) -> None:
    """Run a build step, streaming its output into the build log so a failure explains itself."""
    log(f"$ {label}")
    proc = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    assert proc.stdout is not None
    tail: deque[str] = deque(maxlen=12)

    async def pump() -> None:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            tail.append(line)
            log(line)

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ReleaseBuildError(f"{label} timed out after {timeout}s.") from None
    if proc.returncode != 0:
        # The tail is usually the traceback or the resolver's complaint: enough to see why without the full log.
        detail = "\n".join(tail)
        raise ReleaseBuildError(f"{label} failed with exit {proc.returncode}:\n{detail}")


def _dependency_args(entry: Entry, checkout: Path) -> list[str]:
    found = requirements_for(entry, checkout)
    if found is None:
        return []
    if found.name == "requirements.txt":
        return ["-r", str(found)]
    return [str(found.parent)]


async def _build_venv(settings: Settings, release_dir: Path, entry: Entry, log: BuildLog) -> None:
    """Create the release venv and resolve functions-framework with the function's dependencies in one pass.

    One pass means a pin that conflicts with functions-framework fails the build here, and
    the previous release keeps serving.
    """
    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(settings.uv_cache_dir)
    uv = _uv()
    await _run(
        [uv, "venv", str(release_dir / ".venv"), "--python", sys.executable],
        env,
        settings.build_timeout,
        "uv venv",
        log,
    )
    install = [uv, "pip", "install", "--python", str(Settings.venv_python(release_dir)), RUNTIME_REQUIREMENT]
    install += _dependency_args(entry, release_dir)
    await _run(install, env, settings.build_timeout, "uv pip install -r requirements.txt", log)


async def _discover(
    settings: Settings, release_dir: Path, entry: Entry, overrides: dict[str, str], log: BuildLog
) -> dict[str, Any]:
    """Import the entry module in a subprocess, so nothing it does at import time reaches the supervisor."""
    report = Settings.manifest_file(release_dir)
    env = function_environment(settings, entry.source_dir, overrides)
    args = [str(Settings.venv_python(release_dir)), "-m", "minifaas.discover", str(entry.source_dir)]
    await _run([*args, entry.module, str(report)], env, settings.build_timeout, "discover functions", log)
    return load_manifest(release_dir)


def _check(settings: Settings, entry: Entry, manifest: dict[str, Any]) -> None:
    """Refuse a release with nothing to run, or with a schedule that would never fire."""
    if not manifest["functions"]:
        raise ReleaseBuildError(
            f"{entry.module}.py declares no HTTP or scheduled function. Decorate one with "
            "@functions_framework.http, @https_fn.on_request or @scheduler_fn.on_schedule."
        )
    for spec in manifest["functions"]:
        if spec["trigger"] != "schedule":
            continue
        spec["timezone"] = spec.get("timezone") or settings.timezone
        try:
            schedules.trigger(str(spec["schedule"]), spec["timezone"])
        except schedules.InvalidScheduleError as exc:
            raise ReleaseBuildError(f"{spec['name']}: {exc}") from exc


async def build_release(settings: Settings, overrides: dict[str, str], log: BuildLog) -> Release:
    """Fetch the branch tip and build it. Raises ReleaseBuildError if it isn't usable."""
    settings.releases_dir.mkdir(parents=True, exist_ok=True)
    staging = settings.releases_dir / f".building-{uuid.uuid4().hex[:12]}"

    try:
        log(f"$ git clone --depth 1 --branch {settings.git_branch}")
        sha = await shallow_clone(settings, staging)
        commit = await commit_info(settings, staging)
        log(f"{sha[:12]} {commit['subject']}")

        final = settings.releases_dir / release_id(settings, sha)
        if final.exists():
            log("Already built, reusing it.")
            shutil.rmtree(staging, ignore_errors=True)
            return Release(sha=sha, directory=final, manifest=load_manifest(final))

        entry = find_entry(staging, settings.source_root, settings.entry_module)
        log(f"Functions module: {entry.describe(staging)}")

        await _build_venv(settings, staging, entry, log)
        manifest = await _discover(settings, staging, entry, overrides, log)
        _check(settings, entry, manifest)
        manifest |= {
            "sha": sha,
            "commit": commit,
            "source_root": str(entry.source_dir.relative_to(staging)) or ".",
            "entry_module": entry.module,
        }
        Settings.manifest_file(staging).write_text(json.dumps(manifest, indent=2))

        staging.rename(final)
        names = ", ".join(f"{f['name']} ({f['trigger']})" for f in manifest["functions"])
        log(f"Ready: {names}")
        for item in manifest["unsupported"]:
            log(f"Skipped {item['name']}: {item['reason']} is not supported.")
        return Release(sha=sha, directory=final, manifest=manifest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def prune_releases(settings: Settings, keep: list[str]) -> None:
    """Delete old releases, always keeping the shas named in ``keep``."""
    if not settings.releases_dir.exists():
        return

    protected = {sha for sha in keep if sha}
    candidates = [
        path
        for path in settings.releases_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".") and path.name not in protected
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates[max(0, settings.keep_releases - len(protected)) :]:
        shutil.rmtree(path, ignore_errors=True)
