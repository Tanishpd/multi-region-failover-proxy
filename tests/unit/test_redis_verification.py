"""Redis must be proven to ANSWER, not merely to be configured.

The bug these pin: the prod profile ran Redis with `--requirepass` and gave the
proxy no password. Every command returned NOAUTH. `AuthenticationError` is a
`RedisError`, so the limiter caught it and failed open exactly as designed, and
the service served unlimited traffic while every dashboard read healthy.

`require_redis` could not catch it — it asks whether Redis is CONFIGURED. These
close the gap between configured and working. (ADR-029)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from redis.exceptions import (
    AuthenticationError,
    AuthorizationError,
    NoPermissionError,
    ResponseError,
)
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxy"))

from app.redis_client import (  # noqa: E402
    RedisMisconfigured,
    classify_redis_error,
    verify_redis,
)

pytestmark = pytest.mark.unit


class FakeRedis:
    def __init__(self, raises: BaseException | None = None) -> None:
        self._raises = raises
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        if self._raises is not None:
            raise self._raises
        return True


# --- classification ---------------------------------------------------------


def test_an_auth_error_is_NOT_filed_as_unreachable() -> None:
    """The trap that hid this for a whole production profile.

    `AuthenticationError` subclasses `ConnectionError`, so the natural check —
    catch ConnectionError, call it an outage — files a wrong password under
    "Redis is down" and fails open on it forever.
    """
    assert issubclass(AuthenticationError, RedisConnectionError), (
        "if this ever stops being true the ordering below is no longer load-bearing"
    )
    assert classify_redis_error(AuthenticationError("WRONGPASS")) == "auth"


@pytest.mark.parametrize(
    "exc",
    [
        AuthenticationError("WRONGPASS invalid username-password pair"),
        AuthorizationError("NOPERM"),
        NoPermissionError("NOPERM this user has no permissions to run 'evalsha'"),
        # redis-py does not raise a typed error on every path: a NOAUTH reply to
        # an EVALSHA arrives as a bare ResponseError, so the text matters too.
        ResponseError("NOAUTH Authentication required."),
    ],
)
def test_every_way_redis_says_no_classifies_as_auth(exc: Exception) -> None:
    assert classify_redis_error(exc) == "auth"


@pytest.mark.parametrize(
    "exc",
    [
        RedisConnectionError("Error 111 connecting to redis:6379"),
        RedisTimeoutError("Timeout reading from socket"),
        TimeoutError("asyncio.wait_for expired"),
        OSError("no route to host"),
    ],
)
def test_real_outages_classify_as_unreachable(exc: Exception) -> None:
    """redis-py's ConnectionError/TimeoutError are NOT the builtins. Naming the
    builtins would match nothing and file every outage under `other`."""
    assert classify_redis_error(exc) == "unreachable"


def test_a_rejected_command_is_protocol_not_an_outage() -> None:
    assert classify_redis_error(ResponseError("ERR unknown command 'EVALSHA'")) == "protocol"


# --- boot verification ------------------------------------------------------


async def test_a_healthy_redis_verifies() -> None:
    redis = FakeRedis()
    assert await verify_redis(redis) == "ok"
    assert redis.pings == 1


async def test_a_refused_redis_REFUSES_TO_BOOT() -> None:
    """The whole point. Booting anyway means serving unlimited traffic while
    reporting the limiter as healthy — a credential does not heal itself."""
    with pytest.raises(RedisMisconfigured, match="refused us"):
        await verify_redis(FakeRedis(AuthenticationError("WRONGPASS")))


async def test_the_refusal_names_the_variable_to_check() -> None:
    with pytest.raises(RedisMisconfigured, match="MRFP_REDIS_PASSWORD"):
        await verify_redis(FakeRedis(ResponseError("NOAUTH Authentication required.")))


async def test_an_UNREACHABLE_redis_still_boots() -> None:
    """An outage is precisely what the local fallback exists for, and Redis may
    simply be slower to start than we are. Refusing here would convert a
    designed degradation into a failed deploy. (ADR-013)"""
    assert (
        await verify_redis(FakeRedis(RedisConnectionError("connection refused"))) == "unreachable"
    )


async def test_a_hung_redis_is_unreachable_not_fatal() -> None:
    class Hanging:
        async def ping(self):
            import asyncio

            await asyncio.sleep(10)

    assert await verify_redis(Hanging(), timeout_s=0.05) == "unreachable"


async def test_a_protocol_error_is_also_fatal() -> None:
    """Redis answered and rejected the command. Retrying will not help, and the
    limiter would fail open on it for as long as the deploy lives."""
    with pytest.raises(RedisMisconfigured):
        await verify_redis(FakeRedis(ResponseError("ERR unknown command")))
