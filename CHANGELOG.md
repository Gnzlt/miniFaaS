# Changelog

All notable changes to miniFaaS are listed here. The format follows [Keep a Changelog](https://keepachangelog.com),
and versions follow [Semantic Versioning](https://semver.org). While the major version is 0, a breaking change
bumps the minor version.

## [Unreleased]

## [0.1.0] - 2026-10-21

First public release.

- Serves the HTTP functions of one git repository at `/<name>`, written to the Functions Framework contract
  (Firebase decorators included).
- Fires scheduled functions on the schedules declared in code, as Cloud Scheduler would.
- Redeploys when it finds a new commit: before runs, in the background after requests, on an update schedule and
  on demand. A build that doesn't install or import never goes live, and a worker that keeps crashing rolls back.
- Dashboard with a first-run setup wizard, run history and logs, deploy history, environment variables and settings.
