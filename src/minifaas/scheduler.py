"""Fire scheduled functions, the way Cloud Scheduler fires a Firebase `on_schedule` function.

Each scheduled function gets one APScheduler job. A fire is an HTTP call to the worker, so
the job only decides *when*; the supervisor decides how, and records what happened. Jobs
never overlap themselves, and missed fires collapse into one. One more job, independent of
the release, runs the timed check for new commits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from minifaas import schedules
from minifaas.config import Settings

#: The timed check for new commits. Not a function, so never listed with them.
UPDATE_JOB = "minifaas:update"


class Scheduler:
    def __init__(self, settings: Settings, fire: Callable[[str], Awaitable[None]]) -> None:
        self.settings = settings
        self._fire = fire
        self._scheduler = AsyncIOScheduler(timezone=schedules.zone(settings.timezone))

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def sync(self, functions: list[dict[str, Any]], paused: set[str]) -> None:
        """Replace every function job with the release's schedules, minus the paused ones."""
        self.clear()
        for spec in functions:
            if spec["trigger"] != "schedule" or spec["name"] in paused:
                continue
            self._scheduler.add_job(
                self._fire,
                schedules.trigger(spec["schedule"], spec["timezone"]),
                args=[spec["name"]],
                id=spec["name"],
                max_instances=1,
                coalesce=True,
                misfire_grace_time=self.settings.misfire_grace,
            )

    def clear(self) -> None:
        """Remove the function jobs; the update check stays."""
        for job in self._scheduler.get_jobs():
            if job.id != UPDATE_JOB:
                job.remove()

    def set_update_check(self, expression: str, timezone: str, check: Callable[[], Awaitable[None]]) -> None:
        if self._scheduler.get_job(UPDATE_JOB):
            self._scheduler.remove_job(UPDATE_JOB)
        if schedules.is_off(expression):
            return
        self._scheduler.add_job(
            check,
            schedules.trigger(expression, timezone),
            id=UPDATE_JOB,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

    def next_update_check(self) -> datetime | None:
        job = self._scheduler.get_job(UPDATE_JOB)
        return job.next_run_time if job else None

    def next_runs(self) -> dict[str, datetime | None]:
        return {job.id: job.next_run_time for job in self._scheduler.get_jobs() if job.id != UPDATE_JOB}
