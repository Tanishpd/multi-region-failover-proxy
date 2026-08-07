"""Sentinel support: parsing, pool construction, and boot validation.

The behavioural proof — that a real promotion is survived — is `make
redis-ha-drill`, which needs five containers. These are the parts that can be
pinned without them, and the pool-class assertion is the one that matters:
adopting Sentinel naively swaps a blocking pool for a raising one, and nothing
in a normal test run would notice. (ADR-027)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from redis.asyncio import BlockingConnectionPool
from redis.asyncio.sentinel import SentinelConnectionPool

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxy"))

from app.config import Settings  # noqa: E402
from app.redis_client import (  # noqa: E402
    BlockingSentinelConnectionPool,
    _parse_sentinels,
    build_redis,
)

pytestmark = pytest.mark.unit


def test_sentinel_list_parses_hosts_ports_and_whitespace() -> None:
    assert _parse_sentinels("a:26379, b:26380 ,c:26381") == [
        ("a", 26379),
        ("b", 26380),
        ("c", 26381),
    ]


def test_a_bare_hostname_defaults_to_the_sentinel_port() -> None:
    assert _parse_sentinels("sentinel-1") == [("sentinel-1", 26379)]


@pytest.mark.parametrize("raw", ["", "   ", ",,,"])
def test_an_empty_sentinel_list_is_an_error_not_an_empty_topology(raw: str) -> None:
    """Silently discovering nothing would read as 'Redis is down' on every request."""
    with pytest.raises(ValueError, match="no usable nodes"):
        _parse_sentinels(raw)


def test_a_malformed_entry_names_itself() -> None:
    with pytest.raises(ValueError, match="not host:port"):
        _parse_sentinels("host:not-a-port")


def test_the_sentinel_pool_still_BLOCKS_on_exhaustion() -> None:
    """The regression this file exists for.

    redis-py's SentinelConnectionPool extends the plain ConnectionPool, which
    RAISES when it runs out of connections. The limiter's error path cannot
    distinguish that from a dead Redis, so it would trip the breaker and drop
    every client onto the local bucket during a perfectly healthy burst —
    exactly what ADR-013 uses BlockingConnectionPool to prevent.
    """
    client = build_redis("", sentinels="s1:26379,s2:26379", master_name="mrfp-master")
    pool = client.connection_pool

    assert isinstance(pool, SentinelConnectionPool), "lost sentinel discovery"
    assert isinstance(pool, BlockingConnectionPool), "lost the blocking acquire"
    # The specific method, not just the base class: MRO order decides which one
    # actually runs, and getting it backwards silently restores the raising pool.
    assert BlockingSentinelConnectionPool.get_connection is BlockingConnectionPool.get_connection
    assert pool.max_connections == 128
    assert pool.timeout == 0.05


def test_sentinel_wins_over_a_plain_url() -> None:
    """A deployment with both is telling us it has HA and a convenience URL."""
    client = build_redis("redis://ignored:6379/0", sentinels="s1:26379", master_name="m")
    assert isinstance(client.connection_pool, SentinelConnectionPool)


def test_a_plain_url_still_builds_a_plain_blocking_pool() -> None:
    client = build_redis("redis://127.0.0.1:6380/0")
    assert isinstance(client.connection_pool, BlockingConnectionPool)
    assert not isinstance(client.connection_pool, SentinelConnectionPool)


def test_sentinels_satisfy_require_redis_without_a_url() -> None:
    settings = Settings(redis_url="", redis_sentinels="s1:26379", require_redis=True)
    assert not [p for p in settings.validate_for_boot() if "MRFP_REDIS_URL" in p]


def test_neither_url_nor_sentinels_is_still_a_boot_failure() -> None:
    settings = Settings(redis_url="", redis_sentinels="", require_redis=True)
    assert any("MRFP_REDIS_SENTINELS" in p for p in settings.validate_for_boot())


def test_sentinels_without_a_master_name_fail_at_boot_not_at_first_request() -> None:
    """Otherwise redis-py raises MasterNotFoundError per request, which the
    limiter reads as 'Redis is down' and silently degrades every caller."""
    settings = Settings(redis_sentinels="s1:26379", redis_master_name="", require_redis=True)
    assert any("MRFP_REDIS_MASTER_NAME" in p for p in settings.validate_for_boot())


# --- the prod profile must be able to REACH all of this ---------------------

import re  # noqa: E402

PROD_COMPOSE = (ROOT / "deploy" / "compose.prod.yml").read_text()


def _prod_proxy_env() -> set[str]:
    """MRFP_* keys the prod compose passes to the proxy service."""
    start = PROD_COMPOSE.index("  proxy:")
    end = PROD_COMPOSE.index("\n  us-east-1-endpoint:", start)
    return set(re.findall(r"^\s+(MRFP_[A-Z0-9_]+):", PROD_COMPOSE[start:end], flags=re.M))


def test_the_prod_profile_can_actually_be_pointed_at_sentinel() -> None:
    """Third instance of the same bug class.

    MRFP_STREAMING_ENABLED and MRFP_IDEMPOTENCY_ENABLED were each implemented,
    tested, and unreachable because compose never forwarded them. Sentinel
    support was the third: the prod compose hardcoded MRFP_REDIS_URL as
    required and passed no sentinel variables, so an HA topology could not be
    deployed without editing the file. A feature the deployment cannot select
    is a feature that does not exist.
    """
    env = _prod_proxy_env()
    assert "MRFP_REDIS_SENTINELS" in env
    assert "MRFP_REDIS_MASTER_NAME" in env


def test_the_proxy_is_given_the_redis_password_in_prod() -> None:
    """The silent one.

    Prod runs Redis with --requirepass. If the proxy is not given the password,
    every command returns NOAUTH; `AuthenticationError` is a `RedisError`, so
    the limiter catches it and fails open exactly as designed — and production
    has no rate limiting at all, announced by nothing louder than a
    `rate_limit_degraded` log line.
    """
    assert "MRFP_REDIS_PASSWORD" in _prod_proxy_env()


def test_the_redis_url_is_not_unconditionally_required() -> None:
    """`:?` on the URL makes a Sentinel-only deployment impossible to express."""
    assert "MRFP_REDIS_URL: ${MRFP_REDIS_URL:?" not in PROD_COMPOSE


def test_the_password_reaches_the_plain_url_client_too() -> None:
    """It applied only to the Sentinel path, which is how prod ended up
    unauthenticated on the topology it actually ships."""
    client = build_redis("redis://127.0.0.1:6380/0", password="hunter2")
    assert client.connection_pool.connection_kwargs.get("password") == "hunter2"


def test_an_unset_password_does_not_clobber_one_embedded_in_the_url() -> None:
    client = build_redis("redis://:fromurl@127.0.0.1:6380/0", password="")
    assert client.connection_pool.connection_kwargs.get("password") == "fromurl"
