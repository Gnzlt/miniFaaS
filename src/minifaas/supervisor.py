"""PID 1: keep the worker running on the newest commit that works, fire the schedules, keep the history.

The supervisor never imports the function source. It only talks to git, to uv, and to a
worker subprocess, so nothing a function does at import time can take it, the scheduler or
the dashboard down.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import minifaas
from minifaas import logs
from minifaas.config import ENV_NAME, RESERVED_PREFIXES, Settings, load_settings, save_config
from minifaas.errors import GitError, MiniFaaSError, ReleaseBuildError
from minifaas.gitsync import prepare_credentials, remote_head
from minifaas.release import build_release, function_environment, load_manifest, prune_releases, release_id
from minifaas.scheduler import Scheduler
from minifaas.store import Store

logger = logging.getLogger("minifaas.supervisor")

MAX_BACKOFF_SECONDS = 300.0
BUILD_BACKOFF_SECONDS = 60.0
#: A run checks git first unless a check finished this recently (two schedules firing together check once).
RUN_CHECK_MAX_AGE = 10.0
#: HTTP requests check git in the background at most this often, so a busy endpoint doesn't hammer the remote.
REQUEST_CHECK_MAX_AGE = 60.0
#: How long a run waits for a freshly deployed worker to come up.
WORKER_READY_TIMEOUT = 60.0
#: How much of a response the dashboard's Run now shows.
RESPONSE_PREVIEW = 64 * 1024


@dataclass
class State:
    """What the supervisor believes about the deployment, kept across restarts."""

    #: Release ids (directory names under releases/), not bare commits.
    current: str = ""
    previous: str = ""
    failed_sha: str = ""
    last_error: str = ""
    last_poll: float = 0.0
    paused: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            return cls(**json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, path)


class Hub:
    """Fans live log lines and run events out to every open dashboard stream."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, item: dict[str, Any]) -> None:
        for queue in self.subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(item)

    @contextlib.contextmanager
    def subscribe(self) -> Any:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self.subscribers.add(queue)
        try:
            yield queue
        finally:
            self.subscribers.discard(queue)


class Supervisor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = State()
        self.store: Store | None = None
        self.hub = Hub()
        self.scheduler = Scheduler(settings, self.fire)
        self.manifest: dict[str, Any] = {"functions": [], "unsupported": []}

        self.proc: asyncio.subprocess.Process | None = None
        self.worker_status = "no release"
        self.worker_since = time.time()
        self._reader: asyncio.Task[None] | None = None
        self._token = ""
        self._worker_lock = asyncio.Lock()
        self._consecutive_crashes = 0

        self.build: dict[str, Any] | None = None
        self._deploy_lock = asyncio.Lock()
        self._deploy_now = asyncio.Event()
        self._retry_build_at = 0.0
        self._build_backoff = BUILD_BACKOFF_SECONDS
        self._request_check: asyncio.Task[None] | None = None

        #: Runs in flight, by id → function name, from the worker's own start/end events.
        self.active: dict[str, str] = {}
        self._pending_logs: list[dict[str, Any]] = []
        self._stopping = asyncio.Event()
        self._configured = asyncio.Event()

    # ------------------------------------------------------------------ env set from the dashboard

    def load_env(self) -> dict[str, str]:
        try:
            data = json.loads(self.settings.env_file.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    async def save_env(self, variables: dict[str, str]) -> None:
        for name in variables:
            if not ENV_NAME.match(name) or name.startswith(RESERVED_PREFIXES):
                raise ValueError(f"{name!r} is not a usable variable name.")
        path = self.settings.env_file
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sorted(variables.items())), indent=2))
        tmp.chmod(0o600)
        os.replace(tmp, path)
        logger.info("Environment saved, restarting the worker", extra={"variables": len(variables)})
        if self.state.current:
            await self.restart_worker()

    def environment(self) -> dict[str, str]:
        """What a function sees, for the dashboard's 'missing secrets' check."""
        return function_environment(self.settings, Path("."), self.load_env())

    # ------------------------------------------------------------------ logs and run events

    def _log(self, ts: float, run: str | None, fn: str | None, stream: str, line: str) -> None:
        self._pending_logs.append({"ts": ts, "run": run, "fn": fn, "stream": stream, "line": line})

    def flush_logs(self) -> None:
        if not self._pending_logs or self.store is None:
            return
        entries, self._pending_logs = self._pending_logs, []
        for entry in self.store.add_logs(entries):
            self.hub.publish({"kind": "log", **entry})

    def _system_log(self, ts: float, level: str, line: str) -> None:
        self._log(ts, None, None, "minifaas" if level in ("DEBUG", "INFO") else "minifaas-error", line)

    def _handle_worker_line(self, raw: bytes) -> None:
        text = raw.decode(errors="replace").rstrip("\n")
        try:
            event = json.loads(text) if text.startswith("{") else None
        except json.JSONDecodeError:
            event = None
        if not isinstance(event, dict) or event.get("_mf") != 1:
            # gunicorn itself, or something writing straight to file descriptor 1 or 2.
            self._log(time.time(), None, None, "worker", text)
            return

        kind, ts = event.get("t"), float(event.get("ts", time.time()))
        assert self.store is not None
        if kind == "log":
            self._log(ts, event.get("run"), event.get("fn"), event.get("stream", "stdout"), str(event.get("line")))
        elif kind == "start":
            self.flush_logs()
            self.active[event["run"]] = event["fn"]
            self.store.run_started(event["run"], event["fn"], event["trigger"], self.sha, ts)
            self.hub.publish({"kind": "run", "run": event["run"], "fn": event["fn"], "state": "started"})
            if event["trigger"] == "http":
                self._check_after_request()
        elif kind == "end":
            self.flush_logs()
            self.active.pop(event["run"], None)
            self.store.run_finished(event["run"], event.get("status"), event.get("ms"), event.get("error"), ts)
            self.hub.publish({"kind": "run", "run": event["run"], "fn": event["fn"], "state": "finished"})
        elif kind == "ready":
            self.worker_status, self.worker_since = "running", time.time()
            self._consecutive_crashes = 0
            logger.info("Worker ready", extra={"pid": event.get("pid"), "functions": len(event.get("functions", []))})

    async def _read_worker(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:
                # A line over the stream limit: skip what's left of it rather than stall.
                line = b"[miniFaaS: an over-long output line was dropped]\n"
                with contextlib.suppress(Exception):
                    await proc.stdout.read(proc.stdout._limit)  # type: ignore[attr-defined]
            if not line:
                return
            self._handle_worker_line(line)

    # ------------------------------------------------------------------ releases

    @property
    def sha(self) -> str:
        """The commit that is serving, from the live release's manifest."""
        return str(self.manifest.get("sha", "")) if self.state.current else ""

    def _release_dir(self, release: str) -> Path:
        return self.settings.releases_dir / release

    def _activate(self, release: str) -> None:
        """Point `current` at a release. An atomic swap is the only way a deploy happens."""
        link = self.settings.current_link
        staging = link.with_name(f".current-{os.getpid()}")
        staging.unlink(missing_ok=True)
        staging.symlink_to(Path("releases") / release)
        os.replace(staging, link)

        if self.state.current and self.state.current != release:
            self.state.previous = self.state.current
        self.state.current = release
        self.state.save(self.settings.state_file)
        self.manifest = load_manifest(self._release_dir(release))
        prune_releases(self.settings, [release, self.state.previous])
        logger.info("Release activated", extra={"sha": self.sha[:12]})

    def _adopt_existing_release(self) -> str | None:
        """Reuse whatever `current` already points at, so a container restart doesn't rebuild."""
        link = self.settings.current_link
        if not link.is_symlink():
            return None
        target = link.resolve()
        if not target.is_dir() or not Settings.venv_python(target).exists():
            return None
        return target.name

    def request_deploy(self) -> None:
        self._deploy_now.set()

    async def deploy(self, trigger: str) -> None:
        """Build the branch tip if it is new (or forced from the dashboard) and switch to it."""
        async with self._deploy_lock:
            forced = trigger == "manual"
            self.state.last_poll = time.time()
            try:
                sha = await remote_head(self.settings)
            except GitError as exc:
                logger.warning("Could not reach the remote", extra={"error": str(exc)})
                if forced or not self.state.current:
                    self._record_failed_deploy(trigger, str(exc))
                return

            if sha == self.sha and release_id(self.settings, sha) == self.state.current:
                self.state.save(self.settings.state_file)
                if forced:
                    logger.info("Already up to date", extra={"sha": sha[:12]})
                return
            if sha == self.state.failed_sha and not forced and time.monotonic() < self._retry_build_at:
                return

            assert self.store is not None
            deploy_id = self.store.deploy_started(trigger)
            self.build = {"id": deploy_id, "started": time.time(), "sha": sha, "log": []}
            log_lines: list[str] = self.build["log"]
            logger.info("Building", extra={"sha": sha[:12], "trigger": trigger})
            try:
                release = await build_release(self.settings, self.load_env(), log_lines.append)
            except (GitError, ReleaseBuildError) as exc:
                if isinstance(exc, GitError):
                    log_lines.append(str(exc))
                self.store.deploy_finished(deploy_id, "failed", sha, "", str(exc), "\n".join(log_lines))
                self.state.failed_sha, self.state.last_error = sha, str(exc)
                self.state.save(self.settings.state_file)
                self._retry_build_at = time.monotonic() + self._build_backoff
                self._build_backoff = min(MAX_BACKOFF_SECONDS, self._build_backoff * 2)
                logger.error("Commit rejected, still serving the previous release", extra={"sha": sha[:12]})
                return
            finally:
                self.build = None

            subject = release.manifest.get("commit", {}).get("subject", "")
            self.store.deploy_finished(deploy_id, "ok", release.sha, subject, "", "\n".join(log_lines))
            self._build_backoff = BUILD_BACKOFF_SECONDS
            self.state.failed_sha, self.state.last_error = "", ""
            if release.id != self.state.current:
                await self._drain()
                self._activate(release.id)
                await self.restart_worker()

    async def _drain(self) -> None:
        """Let runs in flight finish before the worker restarts onto a new release.

        Checks now happen when functions run, so a deploy often lands while another function
        is mid-run; restarting at once would cut it off.
        """
        deadline = time.monotonic() + self.settings.function_timeout
        if self.active:
            logger.info("Waiting for runs to finish before switching", extra={"running": len(self.active)})
        while self.active and self.proc is not None and time.monotonic() < deadline:
            await asyncio.sleep(0.5)

    def _record_failed_deploy(self, trigger: str, error: str) -> None:
        assert self.store is not None
        deploy_id = self.store.deploy_started(trigger)
        self.store.deploy_finished(deploy_id, "failed", "", "", error, error)
        self.state.last_error = error

    async def check_for_update(self, trigger: str, max_age: float = 0.0) -> None:
        """Deploy a new commit if there is one: on the update schedule, before a run, after a request.

        Callers that arrive while a check is already going wait for it instead of starting another.
        """
        if not self.settings.configured:
            return
        if self._deploy_lock.locked():
            async with self._deploy_lock:
                return
        if max_age and time.time() - self.state.last_poll < max_age:
            return
        await self.deploy(trigger)

    def _check_after_request(self) -> None:
        """An HTTP request is served on the current code at once; a new commit is live for the next one."""
        if self._request_check and not self._request_check.done():
            return
        if time.time() - self.state.last_poll < REQUEST_CHECK_MAX_AGE:
            return
        self._request_check = asyncio.create_task(self.check_for_update("request", REQUEST_CHECK_MAX_AGE))

    async def _scheduled_check(self) -> None:
        await self.check_for_update("schedule")

    def _schedule_update_check(self) -> None:
        if self.settings.configured:
            self.scheduler.set_update_check(
                self.settings.update_schedule, self.settings.timezone, self._scheduled_check
            )

    async def deploy_loop(self) -> None:
        """The first deploy, and Deploy now. Timed checks and checks before runs call check_for_update."""
        # Until the setup wizard (or the environment) names a repository there is nothing to deploy.
        await self._configured.wait()
        if not self.state.current:
            await self.deploy("boot")
        while not self._stopping.is_set():
            # With nothing serving yet, keep retrying the first deploy with backoff.
            timeout = None if self.state.current else self._build_backoff
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._deploy_now.wait(), timeout=timeout)
            forced = self._deploy_now.is_set()
            self._deploy_now.clear()
            if not self._stopping.is_set():
                await self.deploy("manual" if forced else "retry")

    # ------------------------------------------------------------------ worker

    async def start_worker(self) -> None:
        settings = self.settings
        release_dir = settings.current_link.resolve()
        self.manifest = load_manifest(release_dir)
        source_dir = release_dir / self.manifest.get("source_root", ".")

        self._token = secrets.token_urlsafe(32)
        env = function_environment(settings, source_dir, self.load_env())
        env |= {
            "_MINIFAAS_TOKEN": self._token,
            "_MINIFAAS_MANIFEST": str(Settings.manifest_file(release_dir)),
            "_MINIFAAS_SOURCE_DIR": str(source_dir),
            "_MINIFAAS_ENTRY_MODULE": self.manifest.get("entry_module") or settings.entry_module or "main",
            "_MINIFAAS_BIND": f"{settings.host}:{settings.port}",
            "_MINIFAAS_THREADS": str(settings.threads),
            "_MINIFAAS_GRACE": str(settings.shutdown_grace),
        }
        self.proc = await asyncio.create_subprocess_exec(
            str(Settings.venv_python(release_dir)),
            "-m",
            "minifaas.worker",
            cwd=str(source_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            limit=1024 * 1024,
        )
        self._reader = asyncio.create_task(self._read_worker(self.proc))
        self.worker_status, self.worker_since = "starting", time.time()
        self.scheduler.sync(self.manifest["functions"], set(self.state.paused))
        logger.info("Worker started", extra={"pid": self.proc.pid, "sha": self.sha[:12]})

    async def stop_worker(self) -> None:
        """SIGTERM the worker's process group, then SIGKILL anything still standing."""
        proc, reader = self.proc, self._reader
        self.proc, self._reader = None, None
        self.scheduler.clear()
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.settings.shutdown_grace + 2)
            except TimeoutError:
                logger.warning("Worker ignored SIGTERM, killing it", extra={"pid": proc.pid})
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        if reader is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(reader, timeout=2)
        self._close_active_runs("The worker stopped before this run finished.")
        self.worker_status, self.worker_since = "stopped", time.time()

    def _close_active_runs(self, error: str) -> None:
        self.flush_logs()
        if self.store is not None and self.active:
            self.store.abandon_runs(error)
        self.active.clear()

    async def restart_worker(self) -> None:
        async with self._worker_lock:
            await self.stop_worker()
            self._consecutive_crashes = 0
            await self.start_worker()

    async def watch_worker(self) -> None:
        """Notice a worker that exited on its own, restart it with backoff, roll back if it keeps dying."""
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=0.5)
            self.flush_logs()
            async with self._worker_lock:
                proc = self.proc
                if proc is None or proc.returncode is None:
                    continue
                if self._reader is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self._reader, timeout=2)
                self.proc, self._reader = None, None
                self.scheduler.clear()
                self._close_active_runs("The worker exited before this run finished.")
                self._consecutive_crashes += 1
                self.worker_status, self.worker_since = "crashed", time.time()
                logger.error(
                    "Worker exited",
                    extra={"exit_code": proc.returncode, "consecutive": self._consecutive_crashes},
                )
                if self._consecutive_crashes >= self.settings.max_restarts and await self._roll_back():
                    continue

            delay = min(MAX_BACKOFF_SECONDS, 2.0**self._consecutive_crashes)
            logger.info("Restarting the worker after a backoff", extra={"delay_seconds": delay})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            async with self._worker_lock:
                if not self._stopping.is_set() and self.proc is None and self.state.current:
                    await self.start_worker()

    async def _roll_back(self) -> bool:
        """Fall back to the previous release when the current one cannot stay up."""
        previous = self.state.previous
        if not previous or not self._release_dir(previous).is_dir():
            return False
        logger.warning(
            "Rolling back to the previous release",
            extra={"from_sha": self.sha[:12], "to_sha": previous[:12]},
        )
        self.state.failed_sha = self.sha
        self.state.last_error = "The worker kept crashing on this commit; rolled back."
        self.state.previous = ""
        self._activate(previous)
        self._consecutive_crashes = 0
        await self.start_worker()
        return True

    # ------------------------------------------------------------------ invoking functions

    def spec(self, name: str) -> dict[str, Any] | None:
        return next((f for f in self.manifest["functions"] if f["name"] == name), None)

    def is_running(self, name: str) -> bool:
        return name in self.active.values()

    async def invoke(
        self,
        name: str,
        trigger: str,
        method: str = "POST",
        body: bytes = b"",
        content_type: str = "",
        query: str = "",
    ) -> dict[str, Any]:
        """Call a function through the worker with the supervisor's token, as Cloud Scheduler would."""
        spec = self.spec(name)
        if spec is None:
            raise KeyError(name)
        run_id = uuid.uuid4().hex[:16]
        now = datetime.now(tz=UTC)
        headers = {
            "X-MiniFaaS-Token": self._token,
            "X-MiniFaaS-Trigger": trigger,
            "Function-Execution-Id": run_id,
            "X-CloudScheduler-JobName": f"minifaas-{name}",
            "X-CloudScheduler-ScheduleTime": now.isoformat(timespec="seconds"),
            "User-Agent": f"miniFaaS/{minifaas.__version__}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        url = f"http://127.0.0.1:{self.settings.port}/{name}" + (f"?{query}" if query else "")
        timeout = spec.get("timeout") or self.settings.function_timeout

        def call() -> tuple[int, bytes, str]:
            data = body if method not in ("GET", "HEAD") else None
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.status, response.read(RESPONSE_PREVIEW), response.headers.get_content_type()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read(RESPONSE_PREVIEW), exc.headers.get_content_type()

        if self.worker_status != "running":
            return self._missed(run_id, name, trigger, f"The worker is {self.worker_status}.")
        try:
            status, data, kind = await asyncio.to_thread(call)
        except TimeoutError:
            logger.warning("Function timed out", extra={"function": name, "timeout": timeout})
            return {"run": run_id, "status": None, "error": f"No response after {timeout}s."}
        except OSError as exc:
            return self._missed(run_id, name, trigger, f"Could not reach the worker: {exc}")
        return {"run": run_id, "status": status, "content_type": kind, "body": data.decode(errors="replace")}

    def _missed(self, run_id: str, name: str, trigger: str, error: str) -> dict[str, Any]:
        assert self.store is not None
        self.store.run_failed(run_id, name, trigger, self.sha, error)
        self.hub.publish({"kind": "run", "run": run_id, "fn": name, "state": "finished"})
        logger.error("Run failed", extra={"function": name, "error": error})
        return {"run": run_id, "status": None, "error": error}

    async def _wait_for_worker(self) -> None:
        deadline = time.monotonic() + WORKER_READY_TIMEOUT
        while self.worker_status != "running" and self.proc is not None and time.monotonic() < deadline:
            await asyncio.sleep(0.2)

    async def run_latest(self, name: str, trigger: str, **request: Any) -> dict[str, Any]:
        """Check git, deploy a new commit if there is one, then run the function on whatever is live."""
        await self.check_for_update("run", RUN_CHECK_MAX_AGE)
        await self._wait_for_worker()
        if self.spec(name) is None:
            return self._missed(uuid.uuid4().hex[:16], name, trigger, "The newly deployed commit has no such function.")
        return await self.invoke(name, trigger, **request)

    async def fire(self, name: str) -> None:
        if self.is_running(name):
            logger.warning("Skipped a scheduled run: the previous one is still going", extra={"function": name})
            return
        await self.run_latest(name, "schedule")

    def set_paused(self, name: str, paused: bool) -> None:
        names = set(self.state.paused)
        names = names | {name} if paused else names - {name}
        self.state.paused = sorted(names)
        self.state.save(self.settings.state_file)
        if self.proc is not None:
            self.scheduler.sync(self.manifest["functions"], names)
        logger.info("Schedule paused" if paused else "Schedule resumed", extra={"function": name})

    # ------------------------------------------------------------------ settings from the dashboard

    @property
    def data_volume(self) -> bool:
        """Whether /data is a mount. If not, config, history and builds die with the container."""
        return os.path.ismount(self.settings.data_dir)

    async def reconfigure(self, changes: dict[str, Any], clear_token: bool = False) -> None:
        """Save the Settings page (or the wizard) and apply it without restarting the container."""
        old = self.settings
        new = save_config(old.data_dir, changes, clear_token)
        self.settings = new
        self.scheduler.settings = new
        prepare_credentials(new)
        logging.getLogger().setLevel(new.log_level.upper())
        logger.info("Settings saved", extra={"repository": new.git_url, "branch": new.git_branch})

        self._schedule_update_check()
        repo_fields = ("git_url", "git_branch", "git_token", "git_username", "source_root", "entry_module")
        if any(getattr(old, f) != getattr(new, f) for f in repo_fields) or not self.state.current:
            self.state.failed_sha, self.state.last_error = "", ""
            self.state.save(new.state_file)
            self._build_backoff = BUILD_BACKOFF_SECONDS
            self._retry_build_at = 0.0
            if new.configured:
                self.request_deploy()
        elif self.proc is not None and (old.threads, old.shutdown_grace) != (new.threads, new.shutdown_grace):
            await self.restart_worker()
        if new.configured:
            if self.worker_status == "setup":
                self.worker_status = "no release"
            self._configured.set()

    async def test_connection(self, values: dict[str, Any]) -> dict[str, Any]:
        """Check a repository and branch from the form, using its token or the saved one.

        It runs with a throwaway data dir, so the live askpass files are never rewritten.
        """
        token = values.get("git_token") or self.settings.git_token
        with tempfile.TemporaryDirectory() as tmp:
            try:
                probe = Settings(
                    data_dir=Path(tmp),
                    git_url=str(values.get("git_url") or "").strip(),
                    git_branch=str(values.get("git_branch") or "main").strip(),
                    git_username=str(values.get("git_username") or self.settings.git_username).strip(),
                    git_token=token,
                )
                if not probe.git_url.startswith(("https://", "file://")):
                    return {"ok": False, "error": "The repository URL must start with https://."}
                prepare_credentials(probe)
                sha = await remote_head(probe)
            except (GitError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
        return {"ok": True, "sha": sha}

    # ------------------------------------------------------------------ main loop

    async def retention_loop(self) -> None:
        while not self._stopping.is_set():
            assert self.store is not None
            self.store.prune(self.settings.retention_days)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=3600)

    def bootstrap(self) -> None:
        settings = self.settings
        for path in (settings.data_dir, settings.releases_dir, settings.uv_cache_dir):
            path.mkdir(parents=True, exist_ok=True)
        prepare_credentials(settings)

        self.store = Store(settings.db_file)
        self.store.abandon_runs("miniFaaS restarted before this run finished.")
        logs.attach_sink(lambda ts, level, line: self._system_log(ts, level, line))

        self.state = State.load(settings.state_file)
        existing = self._adopt_existing_release()
        self.state.current = existing or ""
        self.state.save(settings.state_file)
        if existing:
            self.manifest = load_manifest(self._release_dir(existing))
            logger.info("Resuming from the existing release", extra={"sha": self.sha[:12]})
        if settings.configured:
            self._configured.set()
            self._schedule_update_check()
        else:
            self.worker_status = "setup"
            logger.info("No repository configured yet: open the dashboard to set one up")

    @property
    def stopping(self) -> asyncio.Event:
        return self._stopping

    def request_stop(self) -> None:
        logger.info("Shutdown requested")
        self._stopping.set()

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.request_stop)

        self.bootstrap()
        self.scheduler.start()
        if self.state.current and self.settings.configured:
            await self.start_worker()

        tasks = [asyncio.create_task(job()) for job in (self.deploy_loop, self.watch_worker, self.retention_loop)]
        tasks += [asyncio.create_task(job) for job in self.services()]
        try:
            await self._stopping.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            async with self._worker_lock:
                await self.stop_worker()
            self.scheduler.shutdown()
            self.flush_logs()
        return 0

    def services(self) -> list[Any]:
        """Long-running coroutines that live beside the supervisor: the dashboard."""
        from minifaas import dashboard  # imports this module, so not at the top

        return [dashboard.serve(self)]


def main() -> int:
    settings = load_settings()
    logs.configure(settings.log_level)
    try:
        return asyncio.run(Supervisor(settings).run())
    except MiniFaaSError as exc:
        logger.error("Fatal", extra={"error": str(exc)})
        return 1
