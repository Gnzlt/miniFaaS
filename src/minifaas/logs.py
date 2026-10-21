"""The supervisor's own logging: JSON lines on stdout for `docker logs`, and a copy for the dashboard."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

#: Attributes LogRecord always carries; anything else came from an `extra=` and is kept.
_STANDARD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName", "color_message"}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS and not k.startswith("_")}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_extras(record),
        }
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class SinkHandler(logging.Handler):
    """Hands each miniFaaS record to the dashboard as a readable line: `message key=value …`."""

    def __init__(self, sink: Callable[[float, str, str], None]) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            extras = " ".join(f"{k}={v}" for k, v in _extras(record).items())
            line = f"{record.getMessage()} {extras}".strip()
            if record.exc_info:
                line += "\n" + logging.Formatter().formatException(record.exc_info)
            self.sink(record.created, record.levelname, line)
        except Exception:
            self.handleError(record)


def configure(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger, replacing anything already there."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn's access log would put every dashboard poll in the log.
    logging.getLogger("uvicorn.access").disabled = True
    # APScheduler logs every fire at INFO; the run history already says it.
    logging.getLogger("apscheduler").setLevel(max(root.level, logging.WARNING))


def attach_sink(sink: Callable[[float, str, str], None]) -> None:
    """Copy the supervisor's records (not uvicorn's or APScheduler's chatter) to the dashboard."""
    logging.getLogger("minifaas").addHandler(SinkHandler(sink))
