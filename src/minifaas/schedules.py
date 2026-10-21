"""Turn a function's schedule string into an APScheduler trigger, or refuse it at build time.

Firebase's `on_schedule(schedule=...)` is passed to Cloud Scheduler, which takes unix cron
or App Engine's "every N minutes" form. Both are accepted here, so a schedule that deploys
to Firebase also deploys to miniFaaS. A bad one fails the build instead of silently never
firing.
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

_EVERY = re.compile(r"^every\s+(?:(\d+)\s+)?(minutes?|mins?|hours?|hrs?)$", re.IGNORECASE)
_DAILY = re.compile(r"^every\s+day\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)


class InvalidScheduleError(ValueError):
    """A schedule string or time zone could not be parsed."""


def is_off(expression: str) -> bool:
    """An update schedule of "off" (or nothing) means no timed checks."""
    return expression.strip().lower() in ("", "off", "never", "none")


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidScheduleError(f"Unknown time zone {name!r}.") from exc


def trigger(expression: str, timezone: str) -> BaseTrigger:
    tz = zone(timezone)
    text = expression.strip()

    if match := _DAILY.match(text):
        hour, minute = int(match[1]), int(match[2])
        if hour > 23 or minute > 59:
            raise InvalidScheduleError(f"Invalid time in schedule {expression!r}.")
        return CronTrigger(hour=hour, minute=minute, timezone=tz)

    if match := _EVERY.match(text):
        count = int(match[1] or 1)
        if count < 1:
            raise InvalidScheduleError(f"Invalid interval in schedule {expression!r}.")
        if match[2].lower().startswith("h"):
            # Aligned to the clock when it divides the day, as people expect "every 6 hours" to mean 0, 6, 12, 18.
            if 24 % count == 0:
                return CronTrigger(hour=f"*/{count}", minute=0, timezone=tz)
            return IntervalTrigger(hours=count, timezone=tz)
        if 60 % count == 0:
            return CronTrigger(minute=f"*/{count}", timezone=tz)
        return IntervalTrigger(minutes=count, timezone=tz)

    try:
        return CronTrigger.from_crontab(text, timezone=tz)
    except ValueError as exc:
        raise InvalidScheduleError(
            f"Invalid schedule {expression!r}: use 5-field cron, 'every N minutes|hours' or 'every day HH:MM'."
        ) from exc
