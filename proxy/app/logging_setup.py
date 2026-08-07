"""Single-line JSON logs with a request-id bound through a ContextVar.

The event vocabulary is CLOSED (docs/CONTRACT.md section 8). The same strings
are metric label values and the terms used in the docs, so a grep for a term in
the documentation finds the log lines and the metrics too. Do not coin a new
event name without adding it to the contract.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Attributes LogRecord always carries; anything else was passed by us.
_RESERVED = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info thread threadName taskName""".split()
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Built from the epoch directly, not via formatTime(): that goes through
        # time.strftime, which has no %f and would emit a literal "%f" where the
        # milliseconds should be. The chaos harness correlates on these
        # timestamps, so sub-second resolution is not optional.
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn's own access log would emit non-JSON lines into the same stream,
    # and the chaos harness parses this stream with jq.
    for noisy in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True


_log = logging.getLogger("mrfp")


def logx(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event. Never f-string a log message.

    `event` is a closed-vocabulary key, not a sentence: it is used as a metric
    label and grepped by the chaos assertions.
    """
    _log.log(level, event, extra={"event": event, **fields})
