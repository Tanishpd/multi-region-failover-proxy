"""Idempotency claims, so a retry cannot become a second charge.

THE PROBLEM. `MAYBE_SENT` means the upstream may have processed the request and
only the response was lost. Replaying it to another region is the difference
between one generation and two — and against a billed API, between one charge
and two. The proxy already refuses to replay `MAYBE_SENT` without an
`Idempotency-Key`, but that only protects a retry *inside* one request. It does
nothing about the client retrying, or a load balancer re-dispatching, or two
replicas racing on the same key.

WHAT THIS DOES, AND DELIBERATELY DOES NOT. It claims the key in Redis with
SET NX before the upstream call, and marks it done afterwards. A second arrival
for a key still in flight is REFUSED (409) rather than served, because the
honest answer is "your first request may still be running and I will not start a
second one".

It does NOT cache and replay the response body. That is the Stripe-style design
and it is a much bigger thing: a size cap, a TTL policy, a memory budget, an
unanswerable question for streaming responses, and it would turn ADR-014's
deliberately cache-only Redis into a durability dependency. Refusing removes the
double-charge, which is the entire production risk; returning the original
response is a convenience on top.

FAIL-OPEN, matching the limiter. If Redis is unreachable the claim is skipped
and the request proceeds. Losing deduplication degrades a guarantee; refusing
all traffic because a cache is down is an outage. The response says which
happened via `X-Idempotency`.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from enum import Enum

from redis.exceptions import RedisError

from app.logging_setup import logx

# Long enough that a client's own retry window is covered, short enough that a
# cache-only Redis is not asked to remember anything for long.
CLAIM_TTL_S = 900


class ClaimState(str, Enum):
    FRESH = "fresh"  # we own it; proceed
    IN_FLIGHT = "in_flight"  # someone else owns it; refuse
    DONE = "done"  # already completed; refuse
    SKIPPED = "skipped"  # no key, or the store is unreachable


@dataclass(slots=True)
class Claim:
    state: ClaimState
    key: str = ""

    @property
    def may_proceed(self) -> bool:
        return self.state in (ClaimState.FRESH, ClaimState.SKIPPED)


class IdempotencyStore:
    def __init__(self, redis, *, ttl_s: int = CLAIM_TTL_S, enabled: bool = True) -> None:
        self._redis = redis
        self._ttl = ttl_s
        self.enabled = enabled

    @staticmethod
    def _key(principal: str, idempotency_key: str) -> str:
        # Scoped by principal so one tenant cannot probe or collide with
        # another's keys, deliberately or otherwise.
        return f"idem:v1:{{{principal}}}:{idempotency_key}"

    async def claim(self, principal: str, idempotency_key: str) -> Claim:
        if not self.enabled or not idempotency_key or self._redis is None:
            return Claim(ClaimState.SKIPPED)

        key = self._key(principal, idempotency_key)
        try:
            won = await self._redis.set(key, "in_flight", nx=True, ex=self._ttl)
            if won:
                return Claim(ClaimState.FRESH, key)
            current = await self._redis.get(key)
        except (RedisError, TimeoutError, OSError) as exc:
            # Fail open: a dedup cache being down is not a reason to reject
            # traffic. Matches the rate limiter's posture. (ADR-013)
            logx(
                "idempotency_degraded",
                level=logging.WARNING,
                error_class=type(exc).__qualname__,
            )
            return Claim(ClaimState.SKIPPED)

        state = ClaimState.DONE if current == "done" else ClaimState.IN_FLIGHT
        logx("idempotency_replay_refused", level=logging.WARNING, state=state.value)
        return Claim(state, key)

    async def finish(self, claim: Claim) -> None:
        """Mark a claim complete. Best effort — a lost mark only costs a refusal."""
        if claim.state is not ClaimState.FRESH or self._redis is None:
            return
        with contextlib.suppress(RedisError, TimeoutError, OSError):
            await self._redis.set(claim.key, "done", ex=self._ttl)

    async def release(self, claim: Claim) -> None:
        """Release a claim whose request never reached the upstream.

        Without this, a request rejected at admission or killed before its
        first attempt would poison its own key for the full TTL — the client
        retries correctly and is refused for fifteen minutes.
        """
        if claim.state is not ClaimState.FRESH or self._redis is None:
            return
        with contextlib.suppress(RedisError, TimeoutError, OSError):
            await self._redis.delete(claim.key)
