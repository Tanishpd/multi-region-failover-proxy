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

import asyncio

from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.sentinel import Sentinel, SentinelConnectionPool
from redis.exceptions import (
    AuthenticationError,
    AuthorizationError,
    NoPermissionError,
    ResponseError,
)
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

# Message prefixes Redis uses when it is answering but refusing us. redis-py
# raises typed errors for most of these, but not on every path — a NOAUTH reply
# to an EVALSHA arrives as a bare ResponseError — so the text is checked too.
_AUTH_REPLIES = ("NOAUTH", "WRONGPASS", "NOPERM", "ERR AUTH", "ERR Client sent AUTH")


def classify_redis_error(exc: BaseException) -> str:
    """`auth` | `unreachable` | `protocol` | `other`.

    WHY THIS IS NOT OBVIOUS. `AuthenticationError` is a subclass of
    `ConnectionError`, so the natural check — catch ConnectionError, call it an
    outage — silently files a wrong password under "Redis is down". The limiter
    then fails open, which is correct for an outage and completely wrong for a
    credential that will never start working on its own. Production ran with no
    rate limiting behind exactly this. Auth is therefore tested FIRST.
    """
    if isinstance(exc, AuthenticationError | AuthorizationError | NoPermissionError):
        return "auth"
    if isinstance(exc, ResponseError):
        text = str(exc)
        if any(text.startswith(prefix) for prefix in _AUTH_REPLIES):
            return "auth"
        # Redis answered and rejected the command itself: our script or its
        # arguments are wrong. Not an outage, and retrying will not help.
        return "protocol"
    # redis-py's ConnectionError/TimeoutError are NOT the builtins — they derive
    # from RedisError. Naming the builtins here would match nothing and file
    # every genuine outage under "other".
    if isinstance(exc, RedisConnectionError | RedisTimeoutError | TimeoutError | OSError):
        return "unreachable"
    return "other"


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


class RedisMisconfigured(RuntimeError):
    """Redis answered and refused us. Never an outage; never fail-open-able."""


async def verify_redis(redis: Redis, *, timeout_s: float = 2.0) -> str:
    """Prove Redis ANSWERS at boot. Returns `ok` | `unreachable`.

    `require_redis` only ever asked whether Redis was CONFIGURED, never whether
    it replied. That is how a production profile shipped with `--requirepass` on
    the server and no password on the client: every command returned NOAUTH, the
    limiter caught it, failed open exactly as designed, and there was no rate
    limiting at all behind one `rate_limit_degraded` log line.

    So the two failures are separated here rather than merged:

      * refused (auth/permission/protocol) -> RedisMisconfigured. A credential
        does not heal itself, and booting anyway means serving unlimited traffic
        while claiming to be rate limited. Refuse to start.
      * unreachable -> return, and let the process come up. Redis may simply be
        slower to start than we are, and the local fallback is the designed
        behaviour for an outage. (ADR-013)

    The timeout is generous relative to the per-command 50 ms: this runs once,
    against a cold connection, possibly while the whole stack is starting.
    """
    try:
        await asyncio.wait_for(redis.ping(), timeout=timeout_s)
    except Exception as exc:  # re-raised or reported below
        kind = classify_redis_error(exc)
        if kind == "unreachable":
            return "unreachable"
        raise RedisMisconfigured(
            f"Redis answered but refused us ({kind}): {type(exc).__qualname__}: {exc}. "
            "This will not recover on its own, and starting anyway would serve unlimited "
            "traffic while reporting the limiter as healthy. Check MRFP_REDIS_PASSWORD "
            "and any ACL on the account."
        ) from exc
    return "ok"


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

    kwargs = dict(_TUNING)
    # The password applies to BOTH topologies. It previously applied only to the
    # Sentinel path, which made the production profile silently unauthenticated:
    # compose demands MRFP_REDIS_PASSWORD and starts Redis with --requirepass,
    # the operator sets a plain redis://host:6379/0, and every command comes
    # back NOAUTH. `AuthenticationError` is a `RedisError`, so the limiter
    # catches it, fails open exactly as designed, and rate limiting does not
    # exist in production with nothing louder than a `rate_limit_degraded` log.
    #
    # Only set when non-empty, so a password already embedded in the URL is not
    # clobbered by an unset variable.
    if password:
        kwargs["password"] = password

    pool = BlockingConnectionPool.from_url(url, **kwargs)
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
