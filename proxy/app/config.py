"""Proxy settings. Every variable is MRFP_-prefixed; nested ones use `__`.

pydantic-settings SILENTLY IGNORES an unrecognised prefix — a typo'd variable
does not error, it leaves the default in place and you debug the wrong thing.
That is why `main.py` logs the fully-resolved settings object as its first line
on boot: the resolved values are the only trustworthy record of what is active.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BreakerSettings(BaseSettings):
    failure_threshold: int = 5
    cooldown_s: float = 5.0
    max_cooldown_s: float = 60.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MRFP_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # literal = breaker OFF. Every request walks the priority list and really
    # enters the except block. This is the default and the graded configuration
    # for the project brief. (ADR-009)
    mode: str = "literal"

    # No usable default. An unset value must fail loudly rather than silently
    # connecting to the developer's native Redis on host 6379 (ADR-004) — or,
    # as it did before `require_redis` existed, silently disabling distributed
    # rate limiting and falling back to a per-process bucket, so that N replicas
    # enforce N times the configured limit and nothing says so.
    redis_url: str = Field(default="")

    # Set false only for a deliberately Redis-less run (unit tests, a local
    # smoke test). In any real deployment an unset MRFP_REDIS_URL is a
    # misconfiguration, not a mode.
    require_redis: bool = True

    regions_file: str = "/config/regions.yaml"
    region_priority: str = ""

    # Per-attempt timeouts. 200 ms read is the brief's number.
    read_timeout_ms: int = 200
    connect_timeout_ms: int = 100
    pool_timeout_ms: int = 50

    # Per-request ceiling across ALL failover attempts. Distinct from the
    # per-attempt timeouts: httpx bounds each attempt's idleness, this bounds
    # the whole request. 3 attempts x (100 connect + 200 read) = 900.
    total_budget_ms: int = 900

    # Below this, do not start another attempt — there is not enough budget left
    # to finish it, and starting one only delays the 503.
    min_attempt_ms: int = 60

    max_inflight: int = 128
    max_body_bytes: int = 262_144

    # An admission slot is held while the body is read, so an unbounded read is
    # a slot held forever. This bounds it.
    body_read_timeout_s: float = 5.0

    # Guards /admin/* and /debug/*. Empty means those routes are DISABLED —
    # never "open". A control plane that mutates rate-limit state must not be
    # reachable just because someone forgot to set a variable.
    admin_token: str = ""

    # /metrics is separated from the rest of the control plane because scrapers
    # commonly cannot send a bearer token, and it exposes no mutating action.
    metrics_public: bool = True

    trusted_proxies: str = "127.0.0.1/32,::1/128"

    # Token bucket. capacity is the largest burst allowed; refill_per_sec is the
    # sustained rate. Two numbers, because they are two different limits.
    rl_capacity: int = 20
    rl_refill_per_sec: float = 20.0
    rl_ttl_pad_ms: int = 2000
    rl_fail_mode: str = "open"  # open | closed

    log_level: str = "INFO"
    chaos_mode: int = 0
    chaos_token: str = ""

    breaker: BreakerSettings = Field(default_factory=BreakerSettings)

    @property
    def resilient(self) -> bool:
        return self.mode == "resilient"

    def validate_for_boot(self) -> list[str]:
        """Configuration errors that must stop the process, not degrade it.

        pydantic-settings silently ignores an unrecognised variable, so a typo
        leaves a default in place with no error. These checks turn the
        misconfigurations that matter into a refusal to start.
        """
        problems: list[str] = []

        if self.mode not in {"literal", "resilient"}:
            problems.append(f"MRFP_MODE={self.mode!r} is not 'literal' or 'resilient'")

        if self.rl_fail_mode not in {"open", "closed"}:
            problems.append(f"MRFP_RL_FAIL_MODE={self.rl_fail_mode!r} is not 'open' or 'closed'")

        if self.require_redis and not self.redis_url:
            problems.append(
                "MRFP_REDIS_URL is unset. Without it the token bucket is per-process, so N "
                "replicas enforce N times the configured limit. Set it, or set "
                "MRFP_REQUIRE_REDIS=0 to run deliberately without Redis."
            )

        # The budget must fit the walk it is meant to bound, or the last region
        # is silently never tried and failover looks broken for no visible reason.
        per_attempt = self.connect_timeout_ms + self.read_timeout_ms
        needed = per_attempt * 3
        if self.total_budget_ms < needed:
            problems.append(
                f"MRFP_TOTAL_BUDGET_MS={self.total_budget_ms} is below "
                f"3 x (connect {self.connect_timeout_ms} + read {self.read_timeout_ms}) "
                f"= {needed}ms, so the third region can never be attempted"
            )

        if self.chaos_mode and not self.chaos_token:
            problems.append(
                "MRFP_CHAOS_MODE=1 requires an explicit MRFP_CHAOS_TOKEN. The bypass header "
                "skips rate limiting entirely and must never be armed with a default."
            )

        if self.rl_capacity < 1 or self.rl_refill_per_sec <= 0:
            problems.append(
                f"rate limit must be positive: capacity={self.rl_capacity}, "
                f"refill={self.rl_refill_per_sec}"
            )

        return problems

    def priority_override(self) -> list[str]:
        return [r.strip() for r in self.region_priority.split(",") if r.strip()]


settings = Settings()
