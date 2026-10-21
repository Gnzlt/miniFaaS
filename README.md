# miniFaaS

**Python functions from a GitHub repo, served by one small container, over HTTP and on schedules.**

miniFaaS tracks one git repository and runs the functions in it. They're written against Google's
[Functions Framework](https://github.com/GoogleCloudPlatform/functions-framework-python), the same contract as
Cloud Run functions and Firebase. miniFaaS:

- serves each HTTP function at `/<name>`;
- fires each scheduled function on the cron declared in its code;
- redeploys when you push;
- comes with a small dashboard to watch runs, read logs, run a function by hand and set its environment.

```python
# functions/main.py: unchanged from a Firebase project
from firebase_functions import https_fn, scheduler_fn

@https_fn.on_request()
def hello(req: https_fn.Request) -> https_fn.Response:
    return https_fn.Response(f"Hello, {req.args.get('name', 'world')}!")

@scheduler_fn.on_schedule(schedule="every 15 minutes", timezone="America/New_York")
def refresh(event: scheduler_fn.ScheduledEvent) -> None:
    print("refreshing", event.schedule_time)
```

```python
# main.py: plain Functions Framework
import functions_framework

@functions_framework.http
def ping(request):
    return "pong"
```

---

## Quickstart

```bash
docker run -d --name minifaas --restart unless-stopped \
  -p 8080:8080 -p 8081:8081 \
  -v minifaas-data:/data \
  ghcr.io/gnzlt/minifaas:latest
```

Or copy [`docker-compose.yml`](docker-compose.yml) and run `docker compose up -d`.

Then open `http://<host>:8081`. On the first run, the dashboard is a **setup wizard**:

1. Enter the repository URL, the branch, and a token if the repo is private. **Test connection** checks them.
2. Pick the default time zone and how often to check for new commits.
3. Press **Save and deploy**.

The answers go to `/data/config.json` (mode 0600), so **mount a volume on `/data`**. The wizard warns when there's
none, because the settings would be lost with the container. Change them later on the **Settings** page.

You can configure it with environment variables instead (see below). Each `MINIFAAS_*` variable overrides the same
setting from the wizard, and shows as locked on the Settings page.

- Functions are served at `http://<host>:8080/<name>`, for example `curl 'localhost:8080/hello?name=Ana'`.
- The dashboard is at `http://<host>:8081`. **It has no login**, so keep port 8081 on your LAN or behind a proxy that
  has its own access control.
- To deploy, push to the branch. miniFaaS picks the commit up the next time it checks, builds it, checks that it
  imports, and switches to it. It checks:
  - **before every scheduled run, and on Run now**: it looks at git, deploys a new commit if there is one, then runs
    the function on it;
  - **after an HTTP request**: the request is answered at once on the current code, and a check runs in the
    background (at most once a minute), so the next request gets the new commit;
  - **on its update schedule**: at 06:00 and 18:00 by default (`MINIFAAS_UPDATE_SCHEDULE`);
  - **when you press Deploy now** in the dashboard.

  A deploy waits for runs in flight to finish (up to the run timeout) before it switches the worker over.

### Versions

miniFaaS follows [Semantic Versioning](https://semver.org); [`CHANGELOG.md`](CHANGELOG.md) lists what changed. While
the major version is 0, a breaking change bumps the minor version. The running version is shown in the dashboard
header and in `/api/status`.

Each release is a `vX.Y.Z` git tag. GitHub Actions builds it for amd64 and arm64 and publishes it to
`ghcr.io/gnzlt/minifaas`:

| Tag | Moves when |
|---|---|
| `X.Y.Z` | never: one exact release |
| `X.Y` | a patch release comes out |
| `latest` | any new release comes out |

Pin `X.Y` (for example `ghcr.io/gnzlt/minifaas:0.1`) to get fixes without surprises. Pushes to `main` publish nothing.

---

## Writing functions

### Where miniFaaS looks

| Setting | Default |
|---|---|
| Entry module | Found automatically: the `.py` file, anywhere in the repo, that imports `functions_framework` or `firebase_functions` and has decorated functions (`functions/main.py` in a Firebase project). If several files do, the one named `main.py` wins; otherwise pick one with the source folder and entry module settings. |
| Dependencies | The nearest `requirements.txt` (or `pyproject.toml`), from the entry module's folder up to the repo root. They're installed with `uv` into a venv of their own, resolved together with `functions-framework`. |
| Functions | Every function that is decorated with one of the decorators below. Other functions in the module stay private. |

### HTTP functions

Use `@functions_framework.http`, or Firebase's `@https_fn.on_request` / `@https_fn.on_call`.
- **Request:** the function receives a [Flask request](https://flask.palletsprojects.com/api/#flask.Request).
- **Return value:** anything Flask accepts — a string, a dict (sent as JSON), a `(body, status)` tuple or a `Response`.
- **URL:** like on Cloud Functions, `/<name>/anything` reaches the same function, with `request.path == "/anything"`.
- **Errors:** an unhandled exception returns `500` with only the exception type. The traceback goes to the run's log.

### Scheduled functions

Use Firebase's `@scheduler_fn.on_schedule(schedule=..., timezone=...)`. It's the standard way to declare a schedule in
Python code, and the one `firebase deploy` reads. miniFaaS reads it the same way.

- **Schedule:** 5-field cron (`"30 9 * * 1-5"`), `"every N minutes"`, `"every N hours"` or `"every day HH:MM"`.
- **Time zone:** taken from the decorator. Without one, the default is `MINIFAAS_TIMEZONE`.
- **How a run happens:** miniFaaS calls the function the way Cloud Scheduler does, so `event.schedule_time` and
  `event.job_name` are set.
- **No overlap:** a run never starts while the previous one is still going, and missed runs collapse into one.
- **Not public:** scheduled functions don't answer outside requests. Run one by hand from the dashboard.
- **Plain functions:** a plain Functions Framework function has no schedule. Add `firebase-functions` to the
  requirements and decorate the function with `on_schedule`.

A bad cron expression or an unknown time zone fails the deploy, rather than silently never firing.

### Not supported

Firebase event triggers (Firestore, Storage, Pub/Sub, Auth and so on) and CloudEvent functions are skipped, and the
dashboard lists them as skipped. miniFaaS has no event sources besides HTTP and the clock.

### Configuration and secrets

Functions read their settings from environment variables. There are two ways to set them:

- **The container's environment:** the compose file or `--env-file`. Everything except `MINIFAAS_*` is passed to
  the functions.
- **The dashboard's Environment page.** These values are stored in `/data/env.json` (mode 0600), override the
  container's, and restart the worker when saved.

Firebase `secrets=[...]` / `SecretParam("NAME")` read the variable of the same name. The dashboard flags any
secret the code declares that isn't set.

`MINIFAAS_GIT_TOKEN` never reaches a function.

For `firebase_admin` (Firestore, Storage), mount a service-account JSON file and point
`GOOGLE_APPLICATION_CREDENTIALS` at it, as in the compose file.

Functions should be stateless. The checkout a function runs from is replaced on every deploy. Use `/tmp` for
scratch files, and an external store for anything that has to last.

---

## The dashboard

| Page | What it shows |
|---|---|
| **Functions** | The deployed commit, the worker's state and **Deploy now**. Each function's trigger, next run, last run, runs in the last 24 h, and missing secrets. A failed deploy shows why. |
| **Function** | **Run now** (for HTTP functions, with a method, query string and body), **Pause / Resume schedule**, and the run history. Click a run to see its log. |
| **Logs** | A live tail of every function and of miniFaaS itself, with a filter for one function. |
| **Deploys** | Every build with its full log, including failed builds and builds still running. |
| **Environment** | The variables set from the dashboard, and the secret names the code asks for. |
| **Settings** | miniFaaS's own settings, as in the setup wizard. Fields set by an environment variable are locked. The token is write-only: the page shows whether one is saved, never its value. |

The dashboard is plain HTML, CSS and JavaScript, served by the container: no framework, no build step, and no CDN.
Run history and logs are kept for 14 days (`MINIFAAS_RETENTION_DAYS`), with up to 1 MB of log per run.

The same data is available as JSON under `/api/*` on the dashboard port. `GET /api/health` is what the container's
health check calls.

---

## Settings and environment variables

Every setting below can be set on the **Settings** page, which writes `/data/config.json`, or with its environment
variable. **The environment variable wins.** `MINIFAAS_HOST`, `MINIFAAS_PORT`, `MINIFAAS_DASHBOARD_PORT` and
`MINIFAAS_DATA_DIR` are environment-only, because they have to match the container's port mapping and volume.

| Variable | Default | Purpose |
|---|---|---|
| `MINIFAAS_GIT_URL` | *(required)* | HTTPS URL of the repository |
| `MINIFAAS_GIT_BRANCH` | `main` | Branch to track |
| `MINIFAAS_GIT_TOKEN` | — | Token for a private repo; never exposed to functions |
| `MINIFAAS_GIT_USERNAME` | `x-access-token` | Username paired with the token (GitLab: `oauth2`) |
| `MINIFAAS_SOURCE_ROOT` | auto | Folder with the entry module; empty finds it anywhere in the repo |
| `MINIFAAS_ENTRY_MODULE` | auto | Module to import, without `.py`; empty takes the one with decorated functions |
| `MINIFAAS_UPDATE_SCHEDULE` | `0 6,18 * * *` | When to check for new commits, besides before runs (cron, `every day HH:MM`…, in `MINIFAAS_TIMEZONE`); `off` for none |
| `MINIFAAS_TIMEZONE` | `UTC` | Default time zone for schedules |
| `MINIFAAS_PORT` | `8080` | Functions port |
| `MINIFAAS_DASHBOARD_PORT` | `8081` | Dashboard port |
| `MINIFAAS_HOST` | `0.0.0.0` | Bind address for both |
| `MINIFAAS_THREADS` | `8` | Requests served at once |
| `MINIFAAS_FUNCTION_TIMEOUT` | `540` | Seconds the scheduler waits for a run, unless the decorator sets `timeout_sec` |
| `MINIFAAS_MISFIRE_GRACE` | `60` | Seconds a late scheduled run may still start |
| `MINIFAAS_RETENTION_DAYS` | `14` | Days of run history, logs and deploys kept |
| `MINIFAAS_KEEP_RELEASES` | `2` | Releases kept on disk for rollback |
| `MINIFAAS_BUILD_TIMEOUT` | `900` | Seconds allowed for clone + install + discovery |
| `MINIFAAS_MAX_RESTARTS` | `5` | Consecutive worker crashes before rolling back to the previous release |
| `MINIFAAS_SHUTDOWN_GRACE` | `10` | Seconds a stopping worker gets before `SIGKILL` |
| `MINIFAAS_DATA_DIR` | `/data` | Releases, venvs, uv cache, history |
| `MINIFAAS_LOG_LEVEL` | `INFO` | miniFaaS's own log level |

---

## How it works

```
┌─ container ──────────────────────────────────────────────────────────────┐
│  supervisor (PID 1)                                        :8081 dashboard│
│   • checks the branch (git ls-remote) on a schedule and before runs:     │
│     shallow clone → uv venv → install → import in a throwaway process    │
│   • swaps the `current` symlink and restarts the worker, or keeps the    │
│     previous release if any step failed                                  │
│   • fires schedules (APScheduler) as HTTP calls to the worker            │
│   • turns the worker's output into run history and logs (SQLite)         │
│                      │ spawns, reads stdout                               │
│                      ▼                                                    │
│  worker (release venv)                                     :8080 /<name> │
│   • imports your module once; gunicorn, one process, a thread pool       │
│   • tags every printed line with the run that printed it                 │
└──────────────────────────────────────────────────────────────────────────┘
```

```
/data
├── releases/<sha>-<build key>/   one checkout, its .venv and what discovery found
├── current -> releases/<sha>-<build key>
├── uv-cache/         shared, so rebuilds are fast
├── config.json       miniFaaS settings from the wizard / Settings page
├── env.json          functions' variables set from the dashboard
├── state.json        deployed / failed commit, paused schedules
└── minifaas.db       runs, logs, deploys
```

| Situation | What happens |
|---|---|
| A new commit doesn't install or import | Rejected. The previous release keeps serving, and the error is shown in the dashboard. |
| The same broken commit is still at the head of the branch | Retried with backoff, not rebuilt every minute. |
| The worker crashes | Restarted with backoff. If it keeps crashing, miniFaaS rolls back to the previous release. |
| The remote is unreachable | Logged. The current release keeps serving, and the next check tries again. |
| A run outlives its timeout | The scheduler stops waiting and logs it. The run's thread can't be killed, so it keeps going until it returns. |

---

## Development

Requires Python 3.13 and [uv](https://github.com/astral-sh/uv). No Docker needed.

```bash
uv venv venv --python 3.13
uv pip install --python venv/bin/python -r requirements.txt

mkdir /tmp/fnrepo && cp -r examples/firebase /tmp/fnrepo/functions
git -C /tmp/fnrepo init -b main && git -C /tmp/fnrepo add -A && git -C /tmp/fnrepo commit -m initial

MINIFAAS_DATA_DIR=/tmp/minifaas-data PYTHONPATH=src venv/bin/python -m minifaas
```

Then open `http://localhost:8081` and enter `file:///tmp/fnrepo` in the wizard (or start with
`MINIFAAS_GIT_URL=file:///tmp/fnrepo`). Call `curl localhost:8080/hello`, and commit to `/tmp/fnrepo` to watch it deploy.

```bash
uvx ruff check src examples
uvx ruff format src examples
```

## Contributing

Read [`AGENTS.md`](AGENTS.md) first: it lists what the design depends on and how to check a change.

## License

[MIT](LICENSE)
