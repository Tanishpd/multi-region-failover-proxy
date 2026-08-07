"""The Redis connection, tuned so a sick Redis cannot become a latency outage.

Two topologies, one call site:

  * a single URL (`MRFP_REDIS_URL`) — the demo, and the right answer whenever a
    managed Redis is hiding failover behind one stable endpoint;
  * Sentinel (`MRFP_REDIS_SENTINELS` + `MRFP_REDIS_MASTER_NAME`) — self-hosted
    HA, where the client itself must discover which node is currently primary.

See ADR-027 for why Sentinel is supported but not the default, and for the
correction to the earlier blanket rejection of it.
"""

from __future__ import annotations

from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.sentinel import Sentinel, SentinelConnectionPool

# How long a command may take, how long we will wait for a free connection, and
# how long a fresh TCP connect may take. All deliberately tiny: the limiter is
# on the hot path of every request, and a slow Redis must degrade to the local
# bucket rather than add its latency to every response. (ADR-013)
_TUNING = {
    "max_connections": 128,
    "timeout": 0.05,  # how long to wait for a free connection
    "socket_timeout": 0.05,  # per-command ceiling
    "socket_connect_timeout": 0.1,
    # Retrying inside the client hides failures from the breaker, which needs to
    # see them to make a decision.
    "retry_on_timeout": False,
    "health_check_interval": 30,
    "decode_responses": True,
}


class BlockingSentinelConnectionPool(SentinelConnectionPool, BlockingConnectionPool):
    """A Sentinel-aware pool that WAITS for a free connection instead of raising.

    redis-py ships `SentinelConnectionPool` on top of the plain `ConnectionPool`,
    which raises `ConnectionError("Too many connections")` on exhaustion. The
    limiter's error path cannot tell that apart from "Redis is down", so a
    healthy burst that merely needed one more connection would trip the breaker
    and drop every client onto the local bucket — the single-URL path already
    avoids exactly this by using `BlockingConnectionPool`, and the HA path must
    not quietly reintroduce it.

    The mixin is safe because `SentinelConnectionPool` overrides only `reset`,
    `owns_connection` and the discovery helpers, and cooperates via `super()`.
    Connection acquisition — the part that differs — is inherited untouched from
    `BlockingConnectionPool`.
    """


def _parse_sentinels(raw: str) -> list[tuple[str, int]]:
    """`host:port,host:port` → [(host, port)]. Port defaults to 26379."""
    nodes: list[tuple[str, int]] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        host, _, port = item.rpartition(":")
        if not host:  # bare hostname, no colon
            host, port = item, "26379"
        try:
            nodes.append((host, int(port)))
        except ValueError as exc:
            raise ValueError(f"MRFP_REDIS_SENTINELS entry {item!r} is not host:port") from exc
    if not nodes:
        raise ValueError("MRFP_REDIS_SENTINELS was set but parsed to no usable nodes")
    return nodes


def build_redis(
    url: str,
    *,
    sentinels: str = "",
    master_name: str = "mymaster",
    password: str = "",
) -> Redis:
    """Build the client for whichever topology is configured.

    Sentinel wins when configured, because a deployment that has both set is
    telling us it has an HA topology and a convenience URL, and the HA topology
    is the one that survives a node loss.
    """
    if sentinels:
        return _build_sentinel(sentinels, master_name, password)

    pool = BlockingConnectionPool.from_url(url, **_TUNING)
    return Redis(connection_pool=pool)


def _build_sentinel(sentinels: str, master_name: str, password: str) -> Redis:
    nodes = _parse_sentinels(sentinels)

    # The sentinels themselves get their own, even tighter timeouts. Discovery
    # sits in front of every reconnect, so a wedged sentinel must fail fast and
    # let the client try the next one rather than stall the command that
    # triggered the lookup.
    sentinel_kwargs = {"socket_timeout": 0.1, "socket_connect_timeout": 0.1}
    if password:
        sentinel_kwargs["password"] = password

    manager = Sentinel(nodes, sentinel_kwargs=sentinel_kwargs)

    kwargs = dict(_TUNING)
    if password:
        kwargs["password"] = password

    # `master_for` re-resolves the primary whenever it opens a connection, and
    # SentinelConnectionPool drops idle connections when the address changes —
    # which is what turns a promotion into reconnects rather than commands sent
    # to a demoted node.
    return manager.master_for(
        master_name,
        connection_pool_class=BlockingSentinelConnectionPool,
        **kwargs,
    )
