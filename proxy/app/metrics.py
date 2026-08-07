"""Prometheus metrics.

Label VALUES are drawn from the same closed vocabulary as the log events
(docs/CONTRACT.md section 8), so one grep finds the logs, the metrics, and the
prose describing them.

One uvicorn worker is pinned deliberately: prometheus-client without
PROMETHEUS_MULTIPROC_DIR reports only the scraped worker's counters, so a second
worker would silently halve every number in the chaos report. (ADR-008)
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()

# Incremented in the OUTERMOST middleware, so it sees every terminal status —
# including the 429s, 413s, 408s, 404s and 500s that never reach the router.
# proxy_requests_total below is router-scoped and therefore blind to all of
# them; every availability SLI must be computed from THIS one.
HTTP_RESPONSES = Counter(
    "proxy_http_responses_total",
    "Every response the client received, by route, status and client-visible outcome.",
    ["route", "status", "outcome"],
    registry=REGISTRY,
)

POOL_WAITERS = Gauge(
    "proxy_region_pool_waiters",
    "Requests queued for a connection, per region. The LEADING indicator of the "
    "overload -> PoolTimeout -> breaker-opens-on-a-healthy-region feedback loop.",
    ["region"],
    registry=REGISTRY,
)

ADMISSION_INFLIGHT = Gauge(
    "proxy_admission_inflight",
    "Requests currently holding an admission slot.",
    registry=REGISTRY,
)

ADMISSION_REJECTED = Counter(
    "proxy_admission_rejected_total",
    "Requests shed at the admission gate before any upstream work.",
    registry=REGISTRY,
)

REQUESTS = Counter(
    "proxy_requests_total",
    "Requests served, by serving region and status class.",
    ["region", "status"],
    registry=REGISTRY,
)

FAILOVER = Counter(
    "proxy_failover_total",
    "Times the router caught a failure and moved to the next region.",
    ["from_region", "to_region", "code"],
    registry=REGISTRY,
)

ATTEMPT_FAILED = Counter(
    "proxy_attempt_failed_total",
    "Individual upstream attempts that failed, by failure code.",
    ["region", "code"],
    registry=REGISTRY,
)

ALL_DOWN = Counter(
    "proxy_all_regions_down_total",
    "Requests that exhausted every region and returned 503.",
    registry=REGISTRY,
)

# Bucket edges straddle 0.2 so the 200 ms read timeout is visible as a step in
# the histogram rather than being averaged away.
UPSTREAM_LATENCY = Histogram(
    "proxy_upstream_latency_seconds",
    "Per-attempt upstream latency.",
    ["region", "outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.5, 0.9, 1.5),
    registry=REGISTRY,
)

REQUEST_LATENCY = Histogram(
    "proxy_request_latency_seconds",
    "End-to-end proxy latency, including any failover.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.9, 1.5, 3.0),
    registry=REGISTRY,
)

CIRCUIT_STATE = Gauge(
    "proxy_circuit_state",
    "Breaker state: 0 closed, 1 half-open, 2 open.",
    ["region"],
    registry=REGISTRY,
)

REGION_BELIEVED_UP = Gauge(
    "proxy_region_believed_up",
    "Passive health: 1 up, 0 down, -1 never attempted.",
    ["region"],
    registry=REGISTRY,
)

RATE_LIMIT_REJECTED = Counter(
    "rate_limit_rejected_total",
    "Requests rejected with 429.",
    registry=REGISTRY,
)

RATE_LIMIT_DECISIONS = Counter(
    "rate_limit_decisions_total",
    "Rate-limit decisions by which limiter decided: redis, local or open.",
    ["mode"],
    registry=REGISTRY,
)

NOSCRIPT_RECOVERIES = Counter(
    "rate_limit_noscript_recoveries_total",
    "EVALSHA NOSCRIPT recoveries. Nonzero only after a Redis restart or SCRIPT FLUSH.",
    registry=REGISTRY,
)

_STATE_VALUE = {"closed": 0, "half_open": 1, "open": 2}


def observe_breaker(region: str, state: str) -> None:
    CIRCUIT_STATE.labels(region).set(_STATE_VALUE.get(state, 0))


def observe_health(region: str, believed_ok: bool | None) -> None:
    REGION_BELIEVED_UP.labels(region).set(-1 if believed_ok is None else int(believed_ok))


def render() -> bytes:
    return generate_latest(REGISTRY)


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
