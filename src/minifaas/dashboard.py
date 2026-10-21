"""The dashboard: a JSON API over the supervisor, plus the static page that uses it.

It listens on its own port (MINIFAAS_DASHBOARD_PORT) and has no login, so that port is
meant for the LAN or a proxy with its own access control, never the internet. The page
itself is plain HTML, CSS and JavaScript in `static/`, with no build step and nothing
fetched from a CDN.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import minifaas
from minifaas.config import EDITABLE, Settings, SettingsError, locked_fields
from minifaas.gitsync import web_url
from minifaas.supervisor import Supervisor

STATIC = Path(__file__).parent / "static"
KEEPALIVE_SECONDS = 15


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def create_app(sup: Supervisor) -> Starlette:
    # Settings can change from the Settings page, so handlers read sup.settings each time.

    def store() -> Any:
        assert sup.store is not None
        return sup.store

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "worker": sup.worker_status, "sha": sup.sha})

    async def status(request: Request) -> Response:
        env = sup.environment()
        last = store().last_runs()
        stats = store().stats(time.time() - 86400)
        next_runs = sup.scheduler.next_runs()
        functions = []
        for spec in sup.manifest["functions"]:
            name = spec["name"]
            next_run = next_runs.get(name)
            functions.append(
                spec
                | {
                    "paused": name in sup.state.paused,
                    "next_run": next_run.timestamp() if next_run else None,
                    "missing": [key for key in spec.get("secrets", []) if not env.get(key)],
                    "running": sup.is_running(name),
                    "last": last.get(name),
                    "day": stats.get(name, {"runs": 0, "failed": 0}),
                }
            )
        build = sup.build
        settings = sup.settings
        next_check = sup.scheduler.next_update_check()
        return JSONResponse(
            {
                "version": minifaas.__version__,
                "repo": {"url": web_url(settings) or settings.git_url, "branch": settings.git_branch},
                "configured": settings.configured,
                "data_volume": sup.data_volume,
                "release": {
                    "sha": sup.sha,
                    "commit": sup.manifest.get("commit"),
                    "source_root": sup.manifest.get("source_root"),
                }
                if sup.state.current
                else None,
                "failed": {"sha": sup.state.failed_sha, "error": sup.state.last_error}
                if sup.state.last_error
                else None,
                "build": {"id": build["id"], "sha": build["sha"], "started": build["started"]} if build else None,
                "last_check": sup.state.last_poll or None,
                "next_check": next_check.timestamp() if next_check else None,
                "update_schedule": settings.update_schedule,
                "retention_days": settings.retention_days,
                "worker": {"status": sup.worker_status, "since": sup.worker_since},
                "ports": {"functions": settings.port, "dashboard": settings.dashboard_port},
                "functions": functions,
                "unsupported": sup.manifest.get("unsupported", []),
            }
        )

    async def deploy(request: Request) -> Response:
        sup.request_deploy()
        return JSONResponse({"ok": True}, status_code=202)

    async def deploys(request: Request) -> Response:
        return JSONResponse(store().deploys())

    async def deploy_detail(request: Request) -> Response:
        deploy_id = request.path_params["id"]
        build = sup.build
        if build and build["id"] == deploy_id:
            return JSONResponse(
                {"id": deploy_id, "status": "building", "sha": build["sha"], "log": "\n".join(build["log"])}
            )
        row = store().deploy(deploy_id)
        return JSONResponse(row) if row else _error("No such deploy.", 404)

    async def runs(request: Request) -> Response:
        params = request.query_params
        before = float(params["before"]) if params.get("before") else None
        limit = min(int(params.get("limit", 50)), 200)
        return JSONResponse(store().runs(params.get("fn") or None, limit, before))

    async def run_detail(request: Request) -> Response:
        sup.flush_logs()
        row = store().run(request.path_params["id"])
        return JSONResponse(row) if row else _error("No such run.", 404)

    async def run_now(request: Request) -> Response:
        name = request.path_params["name"]
        spec = sup.spec(name)
        if spec is None:
            return _error(f"No function named {name!r} in this release.", 404)
        if spec["trigger"] == "schedule" and sup.is_running(name):
            return _error("It is already running.", 409)
        payload = await request.json() if await request.body() else {}
        method = str(payload.get("method") or "POST").upper()
        result = await sup.run_latest(
            name,
            "manual",
            method=method if spec["trigger"] == "http" else "POST",
            body=str(payload.get("body") or "").encode(),
            content_type=str(payload.get("content_type") or ""),
            query=str(payload.get("query") or "").lstrip("?"),
        )
        return JSONResponse(result)

    async def pause(request: Request) -> Response:
        name, action = request.path_params["name"], request.path_params["action"]
        spec = sup.spec(name)
        if action not in ("pause", "resume"):
            return _error("Not found.", 404)
        if spec is None or spec["trigger"] != "schedule":
            return _error(f"{name!r} is not a scheduled function.", 404)
        sup.set_paused(name, action == "pause")
        return JSONResponse({"ok": True})

    async def logs(request: Request) -> Response:
        sup.flush_logs()
        params = request.query_params
        return JSONResponse(store().logs(params.get("fn") or None, min(int(params.get("limit", 300)), 2000)))

    async def stream(request: Request) -> Response:
        fn = request.query_params.get("fn") or None

        async def events() -> AsyncIterator[str]:
            with sup.hub.subscribe() as queue:
                yield ": connected\n\n"
                idle = 0
                # Ends by itself on shutdown, so uvicorn never has to cancel an open stream.
                while not sup.stopping.is_set():
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=1)
                    except TimeoutError:
                        idle += 1
                        if idle >= KEEPALIVE_SECONDS:
                            idle = 0
                            yield ": keepalive\n\n"
                        continue
                    if fn and item.get("fn") != fn:
                        continue
                    yield f"data: {json.dumps(item, default=str)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    async def env_get(request: Request) -> Response:
        variables = sup.load_env()
        container = sup.environment()
        needed = sorted({key for spec in sup.manifest["functions"] for key in spec.get("secrets", [])})
        return JSONResponse(
            {
                "variables": variables,
                "required": [
                    {
                        "name": key,
                        "source": "dashboard" if key in variables else "container" if container.get(key) else None,
                    }
                    for key in needed
                ],
            }
        )

    async def env_put(request: Request) -> Response:
        payload = await request.json()
        variables = payload.get("variables")
        if not isinstance(variables, dict):
            return _error("Expected {'variables': {name: value}}.", 400)
        try:
            await sup.save_env({str(k).strip(): str(v) for k, v in variables.items()})
        except ValueError as exc:
            return _error(str(exc), 400)
        return JSONResponse({"ok": True})

    async def settings_get(request: Request) -> Response:
        current = sup.settings
        fields = [f for f in EDITABLE if f != "git_token"]
        return JSONResponse(
            {
                "values": {f: getattr(current, f) for f in fields},
                # The token is write-only: the page only learns whether one is saved.
                "token_set": bool(current.git_token),
                "defaults": {f: Settings.model_fields[f].default for f in fields},
                "locked": locked_fields(),
                "configured": current.configured,
                "data_volume": sup.data_volume,
                "config_file": str(current.config_file),
            }
        )

    async def settings_put(request: Request) -> Response:
        payload = await request.json()
        values = payload.get("values")
        if not isinstance(values, dict):
            return _error("Expected {'values': {...}}.", 400)
        locked = locked_fields()
        try:
            await sup.reconfigure(
                {k: v for k, v in values.items() if k not in locked}, clear_token=bool(payload.get("clear_token"))
            )
        except SettingsError as exc:
            return _error(str(exc), 400)
        return JSONResponse({"ok": True})

    async def settings_test(request: Request) -> Response:
        payload = await request.json()
        return JSONResponse(await sup.test_connection(payload if isinstance(payload, dict) else {}))

    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/health", health),
            Route("/api/status", status),
            Route("/api/deploy", deploy, methods=["POST"]),
            Route("/api/deploys", deploys),
            Route("/api/deploys/{id:int}", deploy_detail),
            Route("/api/runs", runs),
            Route("/api/runs/{id}", run_detail),
            Route("/api/functions/{name}/run", run_now, methods=["POST"]),
            Route("/api/functions/{name}/{action:str}", pause, methods=["POST"]),
            Route("/api/logs", logs),
            Route("/api/stream", stream),
            Route("/api/env", env_get),
            Route("/api/env", env_put, methods=["PUT"]),
            Route("/api/settings", settings_get),
            Route("/api/settings", settings_put, methods=["PUT"]),
            Route("/api/settings/test", settings_test, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC)),
        ]
    )


class _Server(uvicorn.Server):
    """uvicorn inside the supervisor's loop: the supervisor owns the signals, not uvicorn."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def serve(sup: Supervisor) -> None:
    config = uvicorn.Config(
        create_app(sup),
        host=sup.settings.host,
        port=sup.settings.dashboard_port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=3,
    )
    server = _Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        await asyncio.shield(serving)
    except asyncio.CancelledError:
        # Let uvicorn finish its own shutdown rather than cancelling it halfway through.
        server.should_exit = True
        await serving
        raise
