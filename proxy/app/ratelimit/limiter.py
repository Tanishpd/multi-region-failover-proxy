"""The rate limiter: key building, the EVALSHA call, and the degraded path."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app import metrics
from app.logging_setup import logx
from app.ratelimit.local_fallback import LocalBucket
from app.ratelimit.scripts import LuaScript

LUA_PATH = Path(__file__).parent / "lua" / "token_bucket.lua"

# Redis must never add latency to the request path. A slow Redis is a worse
# outage than an absent one, because every request pays for it.
REDIS_CALL_TIMEOUT_S = 0.075


@dataclass(slots=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_ms: int
    reset_after_ms: int
    mode: str  # redis | local | open
    bucket_key: str

    @property
    def retry_after_s(self) -> int:
        return max(1, -(-self.retry_after_ms // 1000))  # ceil, minimum 1

    @property
    def reset_after_s(self) -> int:
        return max(0, -(-self.reset_after_ms // 1000))


class TokenBucketLimiter:
    def __init__(
        self,
        redis: Redis | None,
        *,
        capacity: int,
        refill_per_sec: float,
        ttl_pad_ms: int,
        fail_mode: str = "open",
        breaker=None,
    ) -> None:
        self.redis = redis
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self.ttl_pad_ms = ttl_pad_ms
        self.fail_mode = fail_mode
        self.breaker = breaker
        self.script = LuaScript.from_file(redis, LUA_PATH) if redis else None
        self.local = LocalBucket(capacity=capacity, refill_per_sec=refill_per_sec)
        self._recoveries_seen = 0

    @staticmethod
    def bucket_key(identity: str, layer: str = "burst", dimension: str = "ip") -> str:
        # The braces are a Redis hash tag: everything inside determines the
        # slot, so all of one client's layers land together and a multi-key
        # script stays legal if this ever moves to Cluster.
        #
        # `dimension` is ip or key. An authenticated tenant gets its own bucket
        # keyed on the tenant, so its quota follows the account rather than
        # whatever address it happens to call from.
        return f"rl:v1:{{{dimension}:{identity}}}:{layer}"

    async def check(
        self,
        identity: str,
        *,
        cost: int = 1,
        mode: str = "consume",
        dimension: str = "ip",
        capacity: int | None = None,
        refill_per_sec: float | None = None,
    ) -> Decision:
        """Per-principal limits when the caller is authenticated.

        capacity/refill default to the global configuration, so an anonymous
        caller behaves exactly as before and requirement 2 still demonstrates.
        """
        key = self.bucket_key(identity, dimension=dimension)
        cap = capacity if capacity is not None else self.capacity
        rate = refill_per_sec if refill_per_sec is not None else self.refill_per_sec

        if self.redis is not None and (self.breaker is None or self.breaker.allow("redis")[0]):
            try:
                # socket_timeout does not cover pool acquisition, so the whole
                # call is wrapped as well.
                reply = await asyncio.wait_for(
                    self.script(
                        [key],
                        [mode, cost, 0, cap, rate, self.ttl_pad_ms],
                    ),
                    timeout=REDIS_CALL_TIMEOUT_S,
                )
                if self.breaker is not None:
                    self.breaker.record_success("redis")
                decision = self._decode(reply, key)
                metrics.RATE_LIMIT_DECISIONS.labels("redis").inc()
                # A Counter is incremented, never assigned; mirror the script's
                # running total by advancing it by whatever it has not seen yet.
                if self.script.noscript_recoveries > self._recoveries_seen:
                    metrics.NOSCRIPT_RECOVERIES.inc(
                        self.script.noscript_recoveries - self._recoveries_seen
                    )
                    self._recoveries_seen = self.script.noscript_recoveries
                return decision
            except (RedisError, TimeoutError, OSError) as exc:
                if self.breaker is not None:
                    self.breaker.record_failure("redis", code=type(exc).__name__)
                logx(
                    "rate_limit_degraded",
                    level=logging.WARNING,
                    error_class=type(exc).__qualname__,
                    error=str(exc),
                    fail_mode=self.fail_mode,
                )

        return self._degraded(identity, key, cost)

    def _decode(self, reply: list, key: str) -> Decision:
        allowed, _limiting, retry_after_ms, reset_after_ms, _now_ms = reply[:5]
        _tokens_milli, remaining, capacity = reply[5:8]
        return Decision(
            allowed=bool(allowed),
            limit=int(capacity),
            remaining=int(remaining),
            retry_after_ms=int(retry_after_ms),
            reset_after_ms=int(reset_after_ms),
            mode="redis",
            bucket_key=key,
        )

    def _degraded(self, identity: str, key: str, cost: int) -> Decision:
        """Redis is unreachable. Fail open onto a local bucket, or fail closed."""
        if self.fail_mode == "closed":
            return Decision(
                allowed=False,
                limit=self.capacity,
                remaining=0,
                retry_after_ms=1000,
                reset_after_ms=1000,
                mode="open",
                bucket_key=key,
            )

        metrics.RATE_LIMIT_DECISIONS.labels("local").inc()
        allowed, remaining, retry_ms = self.local.consume(identity, cost)
        return Decision(
            allowed=allowed,
            limit=self.capacity,
            remaining=int(remaining),
            retry_after_ms=retry_ms,
            reset_after_ms=int((self.capacity - remaining) / self.refill_per_sec * 1000),
            mode="local",
            bucket_key=key,
        )

    async def peek(self, identity: str) -> Decision:
        return await self.check(identity, cost=0, mode="peek")

    async def reset(self, identity: str) -> bool:
        if self.redis is None:
            return False
        await self.redis.delete(self.bucket_key(identity))
        return True
