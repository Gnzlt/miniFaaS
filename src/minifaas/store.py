"""Run history, logs and deploys, in one SQLite file on the data volume.

Only the supervisor writes here, from its event loop, so a single connection is enough.
Times are Unix seconds. Everything is pruned after MINIFAAS_RETENTION_DAYS.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

#: Log bytes kept per run. Past this a run's output is dropped, with one line saying so.
RUN_LOG_LIMIT = 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deploys (
    id INTEGER PRIMARY KEY,
    sha TEXT,
    subject TEXT,
    trigger TEXT NOT NULL,
    started REAL NOT NULL,
    finished REAL,
    status TEXT NOT NULL,
    error TEXT,
    log TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    fn TEXT NOT NULL,
    trigger TEXT NOT NULL,
    sha TEXT,
    started REAL NOT NULL,
    finished REAL,
    status INTEGER,
    ms INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS runs_fn_started ON runs (fn, started);
CREATE INDEX IF NOT EXISTS runs_started ON runs (started);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    run TEXT,
    fn TEXT,
    stream TEXT NOT NULL,
    line TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_run ON logs (run);
CREATE INDEX IF NOT EXISTS logs_ts ON logs (ts);
"""


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


class Store:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self._log_bytes: dict[str, int] = {}

    # ------------------------------------------------------------------ deploys

    def deploy_started(self, trigger: str) -> int:
        cursor = self.db.execute(
            "INSERT INTO deploys (trigger, started, status) VALUES (?, ?, 'building')", (trigger, time.time())
        )
        return int(cursor.lastrowid or 0)

    def deploy_finished(self, deploy_id: int, status: str, sha: str, subject: str, error: str, log: str) -> None:
        self.db.execute(
            "UPDATE deploys SET finished = ?, status = ?, sha = ?, subject = ?, error = ?, log = ? WHERE id = ?",
            (time.time(), status, sha, subject, error, log, deploy_id),
        )

    def deploys(self, limit: int = 50) -> list[dict[str, Any]]:
        return _rows(
            self.db.execute(
                "SELECT id, sha, subject, trigger, started, finished, status, error FROM deploys "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        )

    def deploy(self, deploy_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM deploys WHERE id = ?", (deploy_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ runs

    def run_started(self, run_id: str, fn: str, trigger: str, sha: str, ts: float) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO runs (id, fn, trigger, sha, started) VALUES (?, ?, ?, ?, ?)",
            (run_id, fn, trigger, sha, ts),
        )

    def run_finished(self, run_id: str, status: int | None, ms: int | None, error: str | None, ts: float) -> None:
        self.db.execute(
            "UPDATE runs SET finished = ?, status = ?, ms = ?, error = ? WHERE id = ? AND finished IS NULL",
            (ts, status, ms, error, run_id),
        )
        self._log_bytes.pop(run_id, None)

    def run_failed(self, run_id: str, fn: str, trigger: str, sha: str, error: str) -> None:
        """A run that never reached the worker, so the history still shows it was due."""
        now = time.time()
        self.run_started(run_id, fn, trigger, sha, now)
        self.run_finished(run_id, None, 0, error, now)

    def abandon_runs(self, error: str) -> None:
        """Close runs whose worker went away before they finished."""
        self.db.execute("UPDATE runs SET finished = ?, error = ? WHERE finished IS NULL", (time.time(), error))
        self._log_bytes.clear()

    def runs(self, fn: str | None = None, limit: int = 50, before: float | None = None) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM runs WHERE started < ?", [before or time.time() + 1]
        if fn:
            query += " AND fn = ?"
            args.append(fn)
        query += " ORDER BY started DESC LIMIT ?"
        return _rows(self.db.execute(query, [*args, limit]))

    def run(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        logs = _rows(self.db.execute("SELECT id, ts, stream, line FROM logs WHERE run = ? ORDER BY id", (run_id,)))
        return dict(row) | {"logs": logs}

    def last_runs(self) -> dict[str, dict[str, Any]]:
        rows = self.db.execute(
            "SELECT r.* FROM runs r JOIN (SELECT fn, MAX(started) AS started FROM runs GROUP BY fn) m "
            "ON r.fn = m.fn AND r.started = m.started"
        )
        return {row["fn"]: dict(row) for row in rows}

    def stats(self, since: float) -> dict[str, dict[str, int]]:
        rows = self.db.execute(
            "SELECT fn, COUNT(*) AS runs, SUM(error IS NOT NULL OR status >= 500) AS failed "
            "FROM runs WHERE started >= ? GROUP BY fn",
            (since,),
        )
        return {row["fn"]: {"runs": row["runs"], "failed": row["failed"] or 0} for row in rows}

    # ------------------------------------------------------------------ logs

    def add_logs(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Insert log lines in one transaction; returns those kept, with their ids."""
        kept = []
        with self.db:
            self.db.execute("BEGIN")
            for entry in entries:
                run = entry.get("run")
                if run:
                    used = self._log_bytes.get(run, 0)
                    if used > RUN_LOG_LIMIT:
                        continue
                    used += len(entry["line"])
                    self._log_bytes[run] = used
                    if used > RUN_LOG_LIMIT:
                        entry = entry | {"line": "[miniFaaS: this run's log is over 1 MB; the rest is dropped]"}
                cursor = self.db.execute(
                    "INSERT INTO logs (ts, run, fn, stream, line) VALUES (?, ?, ?, ?, ?)",
                    (entry["ts"], run, entry.get("fn"), entry["stream"], entry["line"]),
                )
                kept.append(entry | {"id": cursor.lastrowid})
        return kept

    def logs(self, fn: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM logs", []
        if fn:
            query += " WHERE fn = ?"
            args.append(fn)
        rows = _rows(self.db.execute(query + " ORDER BY id DESC LIMIT ?", [*args, limit]))
        return rows[::-1]

    # ------------------------------------------------------------------ retention

    def prune(self, days: int) -> None:
        cutoff = time.time() - days * 86400
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
            self.db.execute("DELETE FROM runs WHERE started < ?", (cutoff,))
            self.db.execute("DELETE FROM deploys WHERE started < ?", (cutoff,))
