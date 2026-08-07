"""Shared fixtures and safety rails.

The most important thing in this file is the Redis guard. This machine runs a
real redis-server on 6379 with real data; our container publishes on 6380. The
`redis_test_client` fixture refuses to hand back a connection unless it is on
db 15 AND a sentinel key written by our own container is present, and it never
calls FLUSHALL. See docs/CONTRACT.md section 10.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator

import pytest

TEST_DB = 15
SENTINEL_KEY = "mrfp:test-guard"
DEFAULT_TEST_URL = f"redis://127.0.0.1:6380/{TEST_DB}"


def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def redis_url() -> str:
    return os.environ.get("MRFP_REDIS_URL_TEST", DEFAULT_TEST_URL)


@pytest.fixture(scope="session")
def redis_available(redis_url: str) -> bool:
    return _port_open("127.0.0.1", 6380)


@pytest.fixture
async def redis_test_client(redis_url: str, redis_available: bool):
    """An async Redis client pinned to db 15, behind two independent guards."""
    if not redis_available:
        pytest.skip("Redis is not listening on 127.0.0.1:6380 — run `make up`")

    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url, decode_responses=True)

    # Guard 1: the URL must actually be db 15. A misconfigured MRFP_REDIS_URL_TEST
    # pointing at db 0 (or at port 6379) must not reach a flush.
    db = client.connection_pool.connection_kwargs.get("db")
    if db != TEST_DB:
        await client.aclose()
        pytest.fail(f"refusing to run: test Redis is db {db!r}, expected {TEST_DB}")

    # Guard 2: the sentinel key is written by our container at startup. Its
    # absence means this is somebody else's Redis, whatever the db index says.
    #
    # It also disappears whenever our Redis restarts, because the container is
    # deliberately cache-only with no persistence (ADR-014) — so a missing
    # sentinel usually means "re-seed", not "wrong server". Say both.
    if not await client.exists(SENTINEL_KEY):
        await client.aclose()
        pytest.fail(
            f"refusing to run: sentinel key {SENTINEL_KEY!r} missing from {redis_url}.\n"
            "Either this is not the MRFP test Redis, or ours restarted and lost it "
            "(the container is cache-only by design).\n"
            "Fix: `make up` — that re-runs the seed container."
        )

    try:
        yield client
    finally:
        # Scoped to our own key prefix. FLUSHALL appears nowhere in this repo.
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor, match="rl:*", count=500)
            if keys:
                await client.delete(*keys)
            if cursor == 0:
                break
        await client.aclose()


@pytest.fixture
def frozen_clock() -> Iterator[list[float]]:
    """A mutable millisecond clock for deterministic refill/breaker tests.

    Bucket and breaker logic take time as an injected value precisely so tests
    never need `sleep()`. Mutate element 0 to advance time.
    """
    now = [1_700_000_000_000.0]
    yield now
