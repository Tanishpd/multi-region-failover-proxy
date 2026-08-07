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
    """Per-process, so N replicas would enforce N x the limit unless divided.

    `replicas` exists for exactly that: with Redis down and 3 replicas, each
    enforces a third of the configured limit, so the fleet total stays close to
    what was asked for. Approximate — traffic is not evenly balanced — but far
    closer than 3x, and the alternative needs the shared store that is by
    definition unavailable in this path.
    """

    capacity: float
    refill_per_sec: float
    replicas: int = 1
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def consume(
        self, key: str, cost: float = 1.0, now_ms: float | None = None
    ) -> tuple[bool, float, int]:
        """Returns (allowed, remaining, retry_after_ms). Same maths as the Lua."""
        now = now_ms if now_ms is not None else time.time() * 1000
        share = max(1.0, self.capacity / max(1, self.replicas))
        rate = self.refill_per_sec / max(1, self.replicas)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=share, updated_ms=now)
            self._buckets[key] = bucket

        elapsed = max(0.0, now - bucket.updated_ms)  # a backwards clock never refunds
        if rate > 0 and elapsed > 0:
            bucket.tokens = min(share, bucket.tokens + elapsed * rate / 1000)
        bucket.updated_ms = now

        if bucket.tokens < cost:
            deficit = cost - bucket.tokens
            retry_ms = int(deficit / rate * 1000) + 1 if rate else 1000
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
