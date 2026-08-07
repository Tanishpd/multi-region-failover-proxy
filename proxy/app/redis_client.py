"""The Redis connection, tuned so a sick Redis cannot become a latency outage."""

from __future__ import annotations

from redis.asyncio import BlockingConnectionPool, Redis


def build_redis(url: str) -> Redis:
    """A pool that waits for a connection rather than raising when it is busy.

    redis-py's default pool RAISES on exhaustion. The limiter's error path would
    read that as "Redis is down", trip the breaker, and drop every client onto
    the local fallback — during a perfectly healthy burst that merely needed one
    more connection. BlockingConnectionPool waits instead, and the short timeout
    keeps that wait bounded.
    """
    pool = BlockingConnectionPool.from_url(
        url,
        max_connections=128,
        timeout=0.05,  # how long to wait for a free connection
        socket_timeout=0.05,  # per-command ceiling
        socket_connect_timeout=0.1,
        # Retrying inside the client hides failures from the breaker, which
        # needs to see them to make a decision.
        retry_on_timeout=False,
        health_check_interval=30,
        decode_responses=True,
    )
    return Redis(connection_pool=pool)
