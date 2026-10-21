"""The process that serves every function in a release.

Run by the supervisor with the release's own interpreter as `python -m minifaas.worker`, so
it may import only the standard library and what functions-framework brings: Flask, Werkzeug
and gunicorn. It imports the entry module once and serves each discovered function at
`/<name>`, the same URL shape as Cloud Functions, on gunicorn with one process and a thread
pool, the way functions-framework runs in production.

It talks to the supervisor through stdout only: every line it writes is a JSON object
marked `"_mf": 1`, either a run event (start, end) or a log line tagged with the run that
printed it. The supervisor turns those into run history and live logs.
"""

from __future__ import annotations

import contextvars
import hmac
import io
import json
import os
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import import_module
from typing import Any

#: A single log line longer than this is cut, so one runaway print can't flood the history.
MAX_LINE = 8000

#: How the supervisor proves a request is its own: scheduled functions only answer to it.
TOKEN_HEADER = "HTTP_X_MINIFAAS_TOKEN"
TRIGGER_HEADER = "HTTP_X_MINIFAAS_TRIGGER"
EXECUTION_HEADER = "HTTP_FUNCTION_EXECUTION_ID"


@dataclass
class Run:
    id: str
    function: str


_current: contextvars.ContextVar[Run | None] = contextvars.ContextVar("minifaas_run", default=None)
_active: dict[str, Run] = {}
_out_lock = threading.Lock()
_out = open(os.dup(1), "w", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the process


def emit(event: dict[str, Any]) -> None:
    line = json.dumps({"_mf": 1, "ts": time.time(), **event}, default=str)
    with _out_lock:
        _out.write(line + "\n")


def _owner() -> Run | None:
    """The run a line belongs to.

    Threads a function starts itself (a ThreadPoolExecutor, say) don't inherit the context,
    so when exactly one run is in flight its stray lines are attributed to it.
    """
    run = _current.get()
    if run is None and len(_active) == 1:
        run = next(iter(_active.values()), None)
    return run


class _Capture(io.TextIOBase):
    """Stands in for sys.stdout / sys.stderr and turns each completed line into a tagged log event."""

    def __init__(self, stream: str, original: io.TextIOBase) -> None:
        self.stream = stream
        self.original = original
        self._pending = threading.local()

    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        # Code that hands the raw descriptor to a subprocess still works; that output arrives untagged.
        return self.original.fileno()

    def write(self, text: str) -> int:
        buffered = getattr(self._pending, "text", "") + text
        *lines, rest = buffered.split("\n")
        self._pending.text = rest
        for line in lines:
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        rest = getattr(self._pending, "text", "")
        if rest:
            self._pending.text = ""
            self._emit(rest)

    def _emit(self, line: str) -> None:
        run = _owner()
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + " …[cut]"
        emit(
            {
                "t": "log",
                "run": run.id if run else None,
                "fn": run.function if run else None,
                "stream": self.stream,
                "line": line,
            }
        )


@dataclass(frozen=True)
class Config:
    token: str
    manifest: dict[str, Any]
    source_dir: str
    entry_module: str
    bind: str
    threads: int
    grace: int


def _read_config() -> Config:
    """Take the private settings out of the environment before any user code can read them."""
    env = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("_MINIFAAS_")}
    with open(env["_MINIFAAS_MANIFEST"], encoding="utf-8") as fh:
        manifest = json.load(fh)
    return Config(
        token=env["_MINIFAAS_TOKEN"],
        manifest=manifest,
        source_dir=env["_MINIFAAS_SOURCE_DIR"],
        entry_module=env["_MINIFAAS_ENTRY_MODULE"],
        bind=env["_MINIFAAS_BIND"],
        threads=int(env["_MINIFAAS_THREADS"]),
        grace=int(env["_MINIFAAS_GRACE"]),
    )


def build_app(config: Config) -> Callable[..., Iterable[bytes]]:
    """Import the functions and wrap them in a WSGI app that routes /<name> to each one."""
    import flask
    from werkzeug.exceptions import HTTPException

    sys.stdout = _Capture("stdout", sys.stdout)
    sys.stderr = _Capture("stderr", sys.stderr)

    sys.path.insert(0, config.source_dir)
    module = import_module(config.entry_module)
    specs = {spec["name"]: spec for spec in config.manifest["functions"]}
    handlers = {name: getattr(module, name) for name in specs}

    app = flask.Flask("minifaas")

    # Like functions-framework, a function owns every path below its name.
    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    def invoke(path: str) -> flask.Response:
        environ = flask.request.environ
        run = Run(id=environ["minifaas.run"], function=environ["minifaas.function"])
        token = _current.set(run)
        _active[run.id] = run
        emit({"t": "start", "run": run.id, "fn": run.function, "trigger": environ["minifaas.trigger"]})
        started = time.perf_counter()
        response = None
        error = None
        try:
            response = app.make_response(handlers[run.function](flask.request._get_current_object()))
        except HTTPException as exc:
            response = exc.get_response()
        except Exception as exc:
            # The traceback goes to the run's log; the caller only learns the exception type.
            traceback.print_exc()
            error = type(exc).__name__
            response = flask.Response(f"Internal Server Error: {error}\n", status=500, mimetype="text/plain")
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            _active.pop(run.id, None)
            _current.reset(token)
            if response is None:
                # SystemExit or another non-Exception escaped the function. It is not swallowed, exactly as in
                # functions-framework, but the run must still be closed: left open, the supervisor would think
                # the function is running for good and skip all its scheduled runs.
                exc_type = sys.exc_info()[0]
                error = exc_type.__name__ if exc_type else "unknown"
            emit(
                {
                    "t": "end",
                    "run": run.id,
                    "fn": run.function,
                    "status": response.status_code if response is not None else None,
                    "ms": round((time.perf_counter() - started) * 1000),
                    "error": error,
                }
            )
        response.headers["Function-Execution-Id"] = run.id
        return response

    def router(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        given = environ.pop(TOKEN_HEADER, "")
        trusted = bool(given) and hmac.compare_digest(given, config.token)
        trigger = environ.pop(TRIGGER_HEADER, "")
        execution = environ.pop(EXECUTION_HEADER, "")

        name, _, rest = environ.get("PATH_INFO", "/").lstrip("/").partition("/")
        spec = specs.get(name)
        # A scheduled function is not a public endpoint, just as on Cloud Scheduler.
        if spec is None or (spec["trigger"] == "schedule" and not trusted):
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Not Found\n"]

        environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + "/" + name
        environ["PATH_INFO"] = "/" + rest
        environ["minifaas.function"] = name
        environ["minifaas.trigger"] = trigger if trusted and trigger in ("schedule", "manual") else "http"
        environ["minifaas.run"] = execution if trusted and execution else uuid.uuid4().hex[:16]
        return app(environ, start_response)

    emit({"t": "ready", "pid": os.getpid(), "functions": sorted(specs)})
    return router


def main() -> int:
    from gunicorn.app.base import BaseApplication

    config = _read_config()

    class Server(BaseApplication):
        def load_config(self) -> None:
            options = {
                "bind": config.bind,
                "workers": 1,
                "threads": config.threads,
                # Long scheduled jobs are normal here; the supervisor bounds them, not gunicorn.
                "timeout": 0,
                "graceful_timeout": config.grace,
                "accesslog": None,
                "errorlog": "-",
                "loglevel": "warning",
            }
            for key, value in options.items():
                self.cfg.set(key, value)

        def load(self) -> Callable[..., Iterable[bytes]]:
            return build_app(config)

    Server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
