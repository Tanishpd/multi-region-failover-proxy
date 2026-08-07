"""An open-loop request pacer.

OPEN-LOOP is the whole point. A closed-loop generator — a fixed pool of workers
that each wait for a response before sending the next request — stops issuing
while the system is stalled. The latency spike during failover therefore never
enters the dataset, and the resilience demo looks better than it is because the
evidence was deleted. That is coordinated omission.

Here, requests are scheduled on a fixed wall-clock cadence (`next_at += 1/rps`)
and dispatched whether or not earlier ones have returned. A request issued behind
schedule records how late it was, and assertion A12 fails the run if the harness
could not sustain 90% of the target rate — a result gathered under a load the
generator could not deliver is not evidence of anything.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx

from chaos.config import FloodConfig, Record


async def _one(
    client: httpx.AsyncClient,
    cfg: FloodConfig,
    seq: int,
    scheduled_at: float,
    out: list[Record],
) -> None:
    rid = uuid.uuid4().hex[:16]
    t_send = time.monotonic()
    headers = {
        "content-type": "application/json",
        # Client-generated, so the proxy's logs can be joined against this CSV.
        "x-request-id": rid,
        # Skips rate limiting when the proxy runs with MRFP_CHAOS_MODE=1, so a
        # flood measures failover rather than the rate limiter.
        "x-chaos-bypass": cfg.bypass_token,
    }
    try:
        resp = await client.post(cfg.url, json=cfg.payload, headers=headers)
        t_recv = time.monotonic()
        out.append(
            Record(
                seq=seq,
                request_id=rid,
                t_send=t_send,
                t_recv=t_recv,
                status=resp.status_code,
                region=resp.headers.get("x-served-by-region", "-"),
                attempts=int(resp.headers.get("x-attempts", "0") or 0),
                failover_path=resp.headers.get("x-failover-path", "-"),
                latency_ms=(t_recv - t_send) * 1000,
                late_by_ms=max(0.0, (t_send - scheduled_at) * 1000),
            )
        )
    except Exception as exc:
        t_recv = time.monotonic()
        out.append(
            Record(
                seq=seq,
                request_id=rid,
                t_send=t_send,
                t_recv=t_recv,
                status=0,
                region="-",
                attempts=0,
                failover_path="-",
                latency_ms=(t_recv - t_send) * 1000,
                error=f"{type(exc).__name__}: {exc}",
                late_by_ms=max(0.0, (t_send - scheduled_at) * 1000),
            )
        )


async def flood(cfg: FloodConfig, stop_after: float | None = None) -> list[Record]:
    """Issue requests at cfg.rps for cfg.duration_s. Returns every record."""
    records: list[Record] = []
    tasks: set[asyncio.Task] = set()
    interval = 1.0 / cfg.rps
    duration = stop_after if stop_after is not None else cfg.duration_s

    # A generous pool: the flood must never be the bottleneck, and a PoolTimeout
    # here would be recorded as a proxy failure when it is really our own.
    limits = httpx.Limits(max_connections=cfg.rps * 4, max_keepalive_connections=cfg.rps * 2)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(cfg.client_timeout_s),
        transport=httpx.AsyncHTTPTransport(limits=limits, retries=0),
    ) as client:
        start = time.monotonic()
        next_at = start
        seq = 0

        while True:
            now = time.monotonic()
            if now - start >= duration:
                break
            if next_at > now:
                await asyncio.sleep(next_at - now)

            task = asyncio.create_task(_one(client, cfg, seq, next_at, records))
            # Held in a set so the task is not garbage collected mid-flight; a
            # bare create_task can be collected before it completes.
            tasks.add(task)
            task.add_done_callback(tasks.discard)

            seq += 1
            next_at += interval

        # Let in-flight requests finish rather than cancelling them: a cancelled
        # request would be recorded as a client error it never actually had.
        if tasks:
            await asyncio.wait(tasks, timeout=cfg.client_timeout_s + 1)

    records.sort(key=lambda r: r.seq)
    return records
