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

#: Unix cron numbers the week from Sunday (0, and 7 as well); the names are the same everywhere.
_WEEKDAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


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


def _unix_weekdays(field: str) -> str:
    """Rewrite a unix cron day-of-week field as weekday names.

    APScheduler counts Monday as 0, where Cloud Scheduler counts it as 1, so passing the digits through would fire a
    day late: `1-5` would run from Tuesday to Saturday. Names mean the same in both.
    """
    if any(char.isalpha() for char in field):
        return field
    days: set[int] = set()
    for part in field.split(","):
        span, slash, step = part.partition("/")
        stride = int(step) if slash else 1
        if span == "*":
            first, last = 0, 6
        elif "-" in span:
            first, last = (int(number) for number in span.split("-"))
        else:
            first = int(span)
            last = 7 if slash else first
        if stride < 1 or not 0 <= first <= last <= 7:
            raise ValueError(f"Invalid day of week {part!r}")
        days.update(day % 7 for day in range(first, last + 1, stride))
    return ",".join(_WEEKDAY_NAMES[day] for day in sorted(days))


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
        fields = text.split()
        if len(fields) != 5:
            raise ValueError("Wrong number of fields")
        minute, hour, day, month, weekday = fields
        return CronTrigger(
            minute=minute, hour=hour, day=day, month=month, day_of_week=_unix_weekdays(weekday), timezone=tz
        )
    except ValueError as exc:
        raise InvalidScheduleError(
            f"Invalid schedule {expression!r}: use 5-field cron, 'every N minutes|hours' or 'every day HH:MM'."
        ) from exc
