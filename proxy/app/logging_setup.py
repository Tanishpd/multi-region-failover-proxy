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

# Field names whose values must never appear in a log line. Redaction is by
# NAME here; `logx` never receives a raw header map, and the proxy does not log
# request headers anywhere — which is what keeps Authorization out.
_REDACT = frozenset(
    {"authorization", "x-api-key", "api_key", "token", "chaos_token", "admin_token", "password"}
)

# Emitted once per process, so there is no volume to control.
_LIFECYCLE = frozenset(
    {
        "boot",
        "redis_verified",
        "redis_refused",
        "api_keys_loaded",
        "admin_disabled",
        "breaker_gossip_subscribed",
        "config_invalid",
        "admission_above_pool",
        "shutdown",
    }
)

_sampling = {"rate": 1.0, "client_ip": "raw"}


def configure_policy(*, log_sample_rate: float = 1.0, log_client_ip: str = "raw") -> None:
    """Sampling and PII policy.

    At 10k rps one line per request is roughly 216 GB/day/pod. Successes are
    sampled; failures and failovers never are — they are the reason the log
    exists, and chaos assertion A13 counts `upstream_failover` lines and would
    fail if any were dropped.
    """
    _sampling["rate"] = log_sample_rate
    _sampling["client_ip"] = log_client_ip


def scrub_ip(value: str) -> str:
    """A client IP is personal data in most jurisdictions."""
    policy = _sampling["client_ip"]
    if policy == "none":
        return "-"
    if policy == "hashed":
        import hashlib

        return "ip_" + hashlib.sha256(value.encode()).hexdigest()[:16]
    return value


def logx(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event. Never f-string a log message.

    `event` is a closed-vocabulary key, not a sentence: it is used as a metric
    label and grepped by the chaos assertions.
    """
    # Sample INFO only. WARNING and above always emit: dropping a failure line
    # to save bytes throws away the one record anybody will want, and A13
    # requires >=95% coverage of upstream_failover.
    #
    # LIFECYCLE events are exempt regardless of level. Sampling exists to bound
    # volume that scales with TRAFFIC; a once-per-process event has a volume of
    # one, so sampling it at 1% simply deletes it. The production profile ships
    # `log_sample_rate=0.01`, and booting it showed zero `boot` and zero
    # `redis_verified` lines — the very event added to tell an operator whether
    # Redis answered was silent in the only profile that matters.
    if event not in _LIFECYCLE and level < logging.WARNING and _sampling["rate"] < 1.0:
        import random

        if random.random() > _sampling["rate"]:
            return

    for name in list(fields):
        if name.lower() in _REDACT:
            fields[name] = "[redacted]"
    for name in ("client_ip", "identity", "peer"):
        if name in fields and isinstance(fields[name], str):
            fields[name] = scrub_ip(fields[name])

    _log.log(level, event, extra={"event": event, **fields})
