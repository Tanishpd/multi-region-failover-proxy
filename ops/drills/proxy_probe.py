"""Pace real requests through the proxy and report what the CLIENT saw.

The Sentinel drill in `redis-ha-drill.sh` measures the redis client in
isolation. This measures the thing that actually matters: whether a Redis
promotion is visible to a caller of `/v1/infer`. It should not be — the limiter
fails open to its local bucket for the few seconds Sentinel needs — and this is
what turns that claim into a measurement.

Open-loop pacing, like `chaos/flood.py`: a closed loop stops issuing requests
while blocked, so any latency spike caused by the failover would erase itself
from the data (coordinated omission).

Emits one JSON object on stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter

import httpx

URL = os.getenv("PROBE_URL", "http://127.0.0.1:8000/v1/infer")
DURATION_S = float(os.getenv("PROBE_DURATION_S", "25"))
RPS = float(os.getenv("PROBE_RPS", "10"))


async def main() -> None:
    statuses: Counter[int] = Counter()
    modes: Counter[str] = Counter()
    latencies: list[float] = []
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:

        async def one() -> None:
            started = time.monotonic()
            try:
                response = await client.post(
                    URL,
                    json={"prompt": "ha-drill", "max_tokens": 4},
                    headers={"x-request-id": f"hadrill-{started:.4f}"},
                )
                statuses[response.status_code] += 1
                # Which limiter served it: `redis` or the local fallback. This
                # is the field that shows the degradation happening at all.
                modes[response.headers.get("x-ratelimit-mode", "-")] += 1
                latencies.append((time.monotonic() - started) * 1000)
            except Exception as exc:  # any client-visible failure counts
                statuses[0] += 1
                if len(errors) < 5:
                    errors.append(f"{type(exc).__qualname__}: {exc}")

        tasks: list[asyncio.Task] = []
        t0 = time.monotonic()
        next_at = t0
        while time.monotonic() - t0 < DURATION_S:
            tasks.append(asyncio.create_task(one()))
            next_at += 1.0 / RPS
            delay = next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        await asyncio.gather(*tasks)

    latencies.sort()
    print(
        json.dumps(
            {
                "total": sum(statuses.values()),
                "statuses": dict(sorted(statuses.items())),
                "ratelimit_modes": dict(modes),
                "client_errors": statuses.get(0, 0),
                "server_errors": sum(v for k, v in statuses.items() if k >= 500),
                "p50_ms": round(latencies[len(latencies) // 2], 1) if latencies else 0.0,
                "max_ms": round(latencies[-1], 1) if latencies else 0.0,
                "sample_errors": errors,
            }
        )
    )


asyncio.run(main())
