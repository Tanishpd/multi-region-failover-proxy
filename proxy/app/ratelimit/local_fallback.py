"""An in-process token bucket, used only when Redis is unreachable.

Doubles as the Python reference implementation of the Lua script's arithmetic,
which is why the two are tested for parity at the interesting boundaries.

Approximate by construction: per-process rather than shared. With one uvicorn
worker that happens to be exact, but the point is degradation, not accuracy —
losing precise limiting is much better than losing the service. (ADR-013)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_ms: float


@dataclass
class LocalBucket:
    capacity: float
    refill_per_sec: float
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def consume(
        self, key: str, cost: float = 1.0, now_ms: float | None = None
    ) -> tuple[bool, float, int]:
        """Returns (allowed, remaining, retry_after_ms). Same maths as the Lua."""
        now = now_ms if now_ms is not None else time.time() * 1000
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self.capacity, updated_ms=now)
            self._buckets[key] = bucket

        elapsed = max(0.0, now - bucket.updated_ms)  # a backwards clock never refunds
        if self.refill_per_sec > 0 and elapsed > 0:
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_sec / 1000)
        bucket.updated_ms = now

        if bucket.tokens < cost:
            deficit = cost - bucket.tokens
            retry_ms = (
                int(deficit / self.refill_per_sec * 1000) + 1 if self.refill_per_sec else 1000
            )
            return False, bucket.tokens, retry_ms

        bucket.tokens -= cost
        return True, bucket.tokens, 0

    def evict_older_than(self, age_ms: float, now_ms: float | None = None) -> int:
        """Idle buckets would otherwise leak; Redis has TTLs, this does not."""
        now = now_ms if now_ms is not None else time.time() * 1000
        stale = [k for k, b in self._buckets.items() if now - b.updated_ms > age_ms]
        for key in stale:
            del self._buckets[key]
        return len(stale)
