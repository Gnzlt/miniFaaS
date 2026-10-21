# AGENTS.md — miniFaaS

Guidance for AI agents and contributors. Read `README.md` first for what miniFaaS does; this file covers how to
change it without breaking what makes it work.

## What this project is

One Docker image that tracks one git repository and serves its Python functions, written to Google's Functions
Framework contract (Firebase decorators included), over HTTP and on the schedules declared in code, with a small
vanilla dashboard. It redeploys itself when it finds a new commit: before a scheduled run or Run now, in the
background after an HTTP request, on its update schedule (twice a day by default), and on Deploy now.

## Architecture in one paragraph

`supervisor.py` is PID 1, running on the image's own dependencies.
- It checks git (`gitsync.py`, `check_for_update`) and builds a candidate release in a staging directory
  (`release.py`).
- It finds the release's functions by importing it in a throwaway subprocess with the release's own interpreter
  (`discover.py`), and only then swaps the `current` symlink and restarts the worker.

`worker.py` runs inside the release venv. It imports the user's module once and serves every function at `/<name>`
on gunicorn. It reports to the supervisor only through JSON lines on stdout: run start and end events, and log lines
tagged with their run.

The supervisor also:
- stores the worker's events in SQLite (`store.py`);
- fires schedules as HTTP calls to the worker (`scheduler.py`, `schedules.py`);
- serves the dashboard API and the static page (`dashboard.py`, `static/`).

## Non-negotiables

Each of these prevents a specific failure. Don't undo one without a very good reason.

- **The supervisor never imports user code.** Discovery and the worker are subprocesses on the release's interpreter,
  so a module that raises, hangs or exits at import can't take down the supervisor, the scheduler or the dashboard.
- **`__init__.py`, `discover.py` and `worker.py` import only the standard library and what functions-framework
  brings (Flask, Werkzeug, gunicorn).** They run in the user's venv, which doesn't have starlette, pydantic or
  APScheduler.
- **The function contract is the standard one. miniFaaS adds no decorators of its own.**
  - Functions are found through `__firebase_endpoint__` and Functions Framework's registry.
  - In-code schedules come from Firebase's `scheduleTrigger`.
  - A scheduled run is an HTTP call with Cloud Scheduler's headers, so Firebase's own wrapper handles it.
- **Validate before swapping.** A commit goes live only after clone, install, discovery and the schedule checks have all
  succeeded. A failed build leaves the previous release serving, deletes its staging directory, and records why in the
  deploys table.
- **The symlink swap is the only way `current` changes**: an atomic `os.symlink` + `os.replace`. Never change a live
  release directory in place.
- **functions-framework and the user's requirements resolve together in one `uv` pass**, in an isolated venv. A
  conflicting pin fails the build. Use `uv` only, never `pip`, for function dependencies.
- **Scheduled functions are not public.** The worker serves them only to requests carrying the per-boot token,
  which it takes out of `os.environ` before user code is imported.
- **Settings come from `config.py` only: environment first, then `/data/config.json`.** An env var always wins
  over the file, and the dashboard shows that field as locked. Host, ports and the data dir are env-only. The
  supervisor starts unconfigured (setup mode) instead of exiting, and `reconfigure()` applies changes live.
- **The token is write-only.** No API response, log or page ever contains `git_token`; the Settings page only learns
  whether one is saved. Test connection uses a throwaway askpass, never the live one.
- **An HTTP request never waits for git.** It is served on the live release and only starts a background check
  (at most once a minute). Scheduled runs and Run now do check first, then run. A deploy waits for runs in flight
  (`_drain`) before switching the worker, because checks now happen exactly when functions are running.
- **A release is keyed by commit + build inputs** (`release_id`: url, source folder, entry module), so changing
  those rebuilds even when the commit is the same.
- **`MINIFAAS_*` variables never reach a function**, and above all `MINIFAAS_GIT_TOKEN`.
  `release.function_environment()` is the one place a function's environment is assembled: the container's
  environment, plus the dashboard's `env.json`.
- **Git is HTTPS-only and shallow, and authenticates through `GIT_ASKPASS`**, which reads a `0600` file. The token
  must never reach a command line, `ps`, a log, or the worker.
- **One worker process serves every function.** Don't add a process per function.
- **The worker binds the functions port directly.** There's no proxy, and a deploy is a restart. The dashboard is on
  its own port.
- **A function that raises must not kill the worker**: it gets a 500 and a traceback in its run's log. A worker that
  dies is restarted with backoff, and rolled back after `MINIFAAS_MAX_RESTARTS` crashes.
- **The dashboard is vanilla.** That means plain HTML, CSS and JavaScript in `static/`: no framework, no build step,
  no CDN, no web fonts. Text goes into the page through `textContent` (the `h()` helper), never `innerHTML`.

## Scope: what miniFaaS is not

Decline these, or move them to a separate project:
- authentication on the dashboard (it's meant for the LAN or a proxy);
- more than one repository per container, or multi-tenancy;
- event triggers other than HTTP and schedules;
- a reverse proxy, autoscaling, or per-function resource limits;
- an external database or queue.

## Code style

- Python 3.13, with `from __future__ import annotations` at the top of every module.
- `ruff` for lint and format, line length 120. `uvx ruff check src examples` and `uvx ruff format src examples`
  must both be clean before committing.
- Type hints on every public signature. Absolute imports from `minifaas`.
- All I/O in the supervisor is async. Subprocesses go through `asyncio.create_subprocess_exec` with an explicit
  argument list, never `shell=True`. The SQLite store is synchronous on purpose: it's small, local, and only the
  supervisor's loop uses it.
- Configuration lives on `Settings` in `config.py`, with the `MINIFAAS_` prefix. No other supervisor module reads
  `os.environ` for configuration. The worker gets its settings as private `_MINIFAAS_*` variables, which it removes
  at start.
- Comments explain *why*, especially where a choice prevents a specific failure.
- The supervisor's runtime dependencies are deliberately four: starlette, uvicorn, apscheduler, pydantic-settings.
  Don't add one without a clear reason.

## Module map

| File | Responsibility |
|---|---|
| `config.py` | `Settings`, every `MINIFAAS_*` variable, `config.json` (load, validate, save), source-root detection |
| `gitsync.py` | `ls-remote`, shallow clone, commit info, credential handling |
| `release.py` | Build, discover and check a release; prune old ones; the function environment |
| `locate.py` | Find the entry module in a checkout by reading files as text (never importing), and its requirements |
| `discover.py` | *(release venv)* Import the entry module and write the manifest of functions |
| `schedules.py` | Schedule strings → APScheduler triggers, refused at build time if invalid |
| `worker.py` | *(release venv)* gunicorn + Flask, `/<name>` routing, the token, per-run log capture |
| `scheduler.py` | One job per scheduled function, synced on every worker start and pause |
| `store.py` | SQLite: deploys, runs, logs, retention |
| `supervisor.py` | Update checks and deploys, symlink swap, worker lifecycle, rollback, invoking functions |
| `dashboard.py` | JSON API, SSE live feed, and the uvicorn server that runs inside the supervisor's loop |
| `static/` | The dashboard page: `index.html`, `app.css`, `app.js` |
| `logs.py` | The supervisor's JSON logging, plus the copy that goes to the dashboard |

## Verifying a change

There's no test suite. Verify by running it, which needs no Docker; see Development in `README.md`. Use both
`examples/firebase` (in a `functions/` folder) and `examples/functions-framework` as `file://` repos. Re-check:

1. With no settings and an empty data dir: the supervisor stays up, every dashboard page is the wizard (with the
   no-volume warning when the data dir isn't a mount), and **Test connection** reports a good repo, a missing branch
   and a bad URL correctly. **Save and deploy** writes `config.json` 0600 and deploys.
   Then, with `MINIFAAS_GIT_URL` set instead: boot builds a release. `/api/status` lists the functions with their triggers, and the worker is `running`.
2. `curl :8080/<http function>` works. A scheduled function returns 404 without the token.
3. A scheduled run fires on time and appears in the history with its log lines. **Run now** and **Pause/Resume** work.
4. After a new commit:
   - a scheduled run, and **Run now**, deploy it first and run the new code (deploy trigger `run`);
   - an HTTP request is answered on the old code, and a later one gets the new code (trigger `request`);
   - **Deploy now** deploys it at once.
5. A syntax error, and a pin that can't be satisfied, are both rejected:
   - the previous sha keeps serving;
   - the deploy shows as failed, with its reason;
   - no `.building-*` directory is left behind.
6. Settings page: an env-set field is locked and wins; a blank token keeps the saved one; changing the source
   folder rebuilds the same commit under a new release id; a bad time zone is refused with a 400.
   A variable saved in the dashboard reaches a function after the restart, and `MINIFAAS_GIT_TOKEN` never appears in
   `/proc/<worker pid>/environ`.
7. A scheduled function that calls `sys.exit(1)` is not handled specially (as in functions-framework), but its run is
   still closed (`error` = `SystemExit`) and its next scheduled run still fires.
8. `kill -9` of the worker → it restarts with backoff. `SIGTERM` to the supervisor → everything stops within the
   grace period, with no orphans and no traceback.
9. Dashboard: open every page in a browser with the console open. There are no errors, and the layout holds at
   phone and desktop widths.

`.github/workflows/docker.yml` builds the image and smoke-tests it on every push and pull request, and publishes it to
`ghcr.io/gnzlt/minifaas` when a release tag is pushed.

## Git

- Imperative commit subjects. Keep changes scoped.
- Don't commit `venv/`, `/data/`, or anything under a release directory.
- Changes reach `main` through pull requests. A push to `main` builds and smoke-tests the image but publishes
  nothing: only a `vX.Y.Z` tag does.

## Releasing

The version is written once, as `__version__` in `src/minifaas/__init__.py`. `pyproject.toml` reads it from there.

1. Bump `__version__`.
2. In `CHANGELOG.md`, rename `[Unreleased]` to the new version and date, and add a fresh empty `[Unreleased]`.
3. Commit as `Release X.Y.Z`, then tag it: `git tag -a vX.Y.Z -m "miniFaaS X.Y.Z"` and `git push origin vX.Y.Z`.

The workflow fails if the tag and `__version__` disagree. Otherwise it publishes `X.Y.Z`, `X.Y` and `latest` (plus `X`
from 1.0; a prerelease such as `0.2.0-rc.1` gets only its own tag) and creates the GitHub Release from the changelog.

A change is breaking if it touches the names or defaults of `MINIFAAS_*` variables, the `config.json` or `/data` layout,
the dashboard API, or the function contract. While the major version is 0, a breaking change bumps the minor version.
