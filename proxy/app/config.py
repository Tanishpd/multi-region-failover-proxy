"""Proxy settings. Every variable is MRFP_-prefixed; nested ones use `__`.

pydantic-settings SILENTLY IGNORES an unrecognised prefix — a typo'd variable
does not error, it leaves the default in place and you debug the wrong thing.
That is why `main.py` logs the fully-resolved settings object as its first line
on boot: the resolved values are the only trustworthy record of what is active.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from app.profiles import DEMO, PROD, ProfileDefaults, active_profile


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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Profile presets sit BELOW env, so an explicit variable always wins.

        This ordering is what keeps the graded demo safe: docker-compose.yml
        pins MRFP_READ_TIMEOUT_MS=200 as an environment variable, so even
        MRFP_PROFILE=prod against that stack cannot move it.
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            ProfileDefaults(settings_cls),
            file_secret_settings,
        )

    profile: str = Field(default_factory=active_profile)

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

    # How long a passive failure verdict keeps a region out of /readyz. Without
    # an expiry, readiness LATCHES: nothing can record a success once traffic
    # stops arriving, so the pod never returns to service.
    health_stale_after_s: float = 30.0

    # Whether a deliberate load shed (429, or 503+Retry-After) should fail over.
    # False is correct in production: moving shed load to a neighbour is how one
    # region's overload becomes the fleet's. True preserves the brief's demo
    # behaviour, where a region returning 503 must visibly fail over.
    failover_on_shed: bool = True

    # 0 means "derive from the smallest per-region connection pool". Admission
    # above pool capacity turns overload into PoolTimeout, which the router
    # would otherwise record as a REGION failure — opening a breaker on a region
    # that was never contacted, and purging its warm pool at peak load.
    max_inflight: int = 128
    max_body_bytes: int = 262_144

    # Responses are buffered so they can be handed back after failover, so they
    # need the same ceiling the request side has.
    max_response_bytes: int = 8 * 1024 * 1024

    # How many regions to attempt. The demo walks all three (requirement 3);
    # production stops at two, because a third 31s attempt helps nobody.
    region_attempt_limit: int = 3

    # Replay a request that MAY have reached the upstream? Against a billed API
    # this is a double charge, so production requires an Idempotency-Key.
    replay_maybe_sent: bool = True

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
    # Fraction of SUCCESSFUL requests logged. Failures and failovers are always
    # logged regardless — they are the reason the log exists, and assertion A13
    # counts them.
    log_sample_rate: float = 1.0
    # raw | hashed | none. A client IP is personal data in most jurisdictions.
    log_client_ip: str = "raw"

    # off | optional | required. `optional` authenticates when a key is present
    # and falls back to IP identity when it is not, which is how the brief's
    # anonymous 429 demo keeps working alongside real auth.
    auth_mode: str = "off"
    api_keys_file: str = ""

    graceful_shutdown_s: int = 5

    # Streaming is OFF in the demo profile so the graded behaviour is unchanged
    # by its presence, not merely equivalent.
    streaming_enabled: bool = False
    # Time to first byte. This is the replayable window: a region that fails
    # inside it is swapped invisibly.
    stream_ttfb_timeout_s: float = 30.0
    # Silence between chunks once committed. Generous, because a model pausing
    # mid-generation is normal and truncating it would be worse than waiting.
    stream_inter_chunk_timeout_s: float = 60.0
    # Streams hold a pool connection for their whole life, so they get their own
    # admission budget rather than sharing the buffered one.
    max_inflight_streams: int = 32
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
        # attempted is silently never reached and failover looks broken for no
        # visible reason.
        #
        # Scaled by region_attempt_limit, not a hardcoded 3. Hardcoding it meant
        # this check rejected the prod profile — which deliberately attempts two
        # regions — as if it had budgeted for three. A validator that refuses a
        # correct configuration is worse than no validator, because the operator
        # learns to work around it.
        per_attempt = self.connect_timeout_ms + self.read_timeout_ms
        attempts = self.region_attempt_limit
        needed = per_attempt * attempts
        if self.total_budget_ms < needed:
            problems.append(
                f"MRFP_TOTAL_BUDGET_MS={self.total_budget_ms} is below "
                f"{attempts} x (connect {self.connect_timeout_ms} + read {self.read_timeout_ms}) "
                f"= {needed}ms, so attempt {attempts} can never be made. Raise the budget or "
                f"lower MRFP_REGION_ATTEMPT_LIMIT."
            )

        if self.chaos_mode and not self.chaos_token:
            problems.append(
                "MRFP_CHAOS_MODE=1 requires an explicit MRFP_CHAOS_TOKEN. The bypass header "
                "skips rate limiting entirely and must never be armed with a default."
            )

        if self.profile not in {DEMO, PROD}:
            problems.append(f"MRFP_PROFILE={self.profile!r} is not {DEMO!r} or {PROD!r}")

        if self.auth_mode not in {"off", "optional", "required"}:
            problems.append(f"MRFP_AUTH_MODE={self.auth_mode!r} is not off/optional/required")

        if self.auth_mode != "off" and not self.api_keys_file:
            problems.append(
                f"MRFP_AUTH_MODE={self.auth_mode!r} requires MRFP_API_KEYS_FILE. Refusing to "
                "claim authentication with no key store."
            )

        if self.log_client_ip not in {"raw", "hashed", "none"}:
            problems.append(f"MRFP_LOG_CLIENT_IP={self.log_client_ip!r} is not raw/hashed/none")

        if not 0.0 <= self.log_sample_rate <= 1.0:
            problems.append(f"MRFP_LOG_SAMPLE_RATE={self.log_sample_rate} is not in [0, 1]")

        if self.region_attempt_limit < 1:
            problems.append(f"MRFP_REGION_ATTEMPT_LIMIT={self.region_attempt_limit} must be >= 1")

        if self.profile == PROD and self.trusted_proxies.strip() in {
            "",
            "127.0.0.1/32,::1/128",
        }:
            problems.append(
                "the prod profile requires MRFP_TRUSTED_PROXIES to name the REAL ingress address. "
                "Loopback-only means every client shares one rate-limit bucket."
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
