"""The Lua token bucket, against a REAL Redis.

fakeredis is not used here on purpose: the Lua semantics — float truncation on
the RESP boundary, %.14g formatting, TIME resolution, single-threaded execution
— are precisely what is under test, so simulating them would be circular.

Every test drives time via the now_override ARGV rather than sleeping, so the
suite is deterministic and fast. Guards on the Redis connection (db 15 plus a
container-written sentinel) live in tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxy"))

from app.ratelimit.scripts import LuaScript  # noqa: E402

pytestmark = pytest.mark.integration

LUA = (ROOT / "proxy" / "app" / "ratelimit" / "lua" / "token_bucket.lua").read_text()

CAPACITY = 20
RATE = 20.0
PAD_MS = 2000
T0 = 1_700_000_000_000


def call(script, key, *, now, cost=1, mode="consume", cap=CAPACITY, rate=RATE):
    return script([key], [mode, cost, now, cap, rate, PAD_MS])


def decode(reply):
    allowed, limiting, retry_ms, reset_ms, now_ms = reply[:5]
    tokens_milli, remaining, capacity = reply[5:8]
    return {
        "allowed": bool(allowed),
        "limiting": limiting,
        "retry_ms": retry_ms,
        "reset_ms": reset_ms,
        "now_ms": now_ms,
        "tokens": tokens_milli / 1000,
        "remaining": remaining,
        "capacity": capacity,
    }


@pytest.fixture
async def script(redis_test_client):
    return LuaScript(redis_test_client, LUA)


@pytest.fixture
def key(request):
    return f"rl:v1:{{test:{request.node.name}}}:burst"


async def test_first_sight_is_a_full_bucket(script, key) -> None:
    r = decode(await call(script, key, now=T0))
    assert r["allowed"]
    assert r["capacity"] == CAPACITY
    assert r["tokens"] == pytest.approx(CAPACITY - 1)


async def test_capacity_is_the_burst_limit(script, key) -> None:
    for _ in range(CAPACITY):
        assert decode(await call(script, key, now=T0))["allowed"]
    denied = decode(await call(script, key, now=T0))
    assert not denied["allowed"]
    assert denied["retry_ms"] > 0


async def test_refill_is_linear_in_elapsed_time(script, key) -> None:
    for _ in range(CAPACITY):
        await call(script, key, now=T0)
    # 500 ms at 20/s = 10 tokens back.
    r = decode(await call(script, key, now=T0 + 500))
    assert r["allowed"]
    assert r["tokens"] == pytest.approx(9.0, abs=0.001)


async def test_refill_clamps_at_capacity(script, key) -> None:
    """An idle hour must not accrue 72,000 tokens."""
    await call(script, key, now=T0)
    r = decode(await call(script, key, now=T0 + 3_600_000))
    assert r["tokens"] == pytest.approx(CAPACITY - 1)


async def test_fractional_tokens_survive_a_100x10ms_loop(script, key) -> None:
    """THE subtle one.

    At 20 tokens/s a 10 ms gap accrues 0.2 tokens. If the script stored integers
    that would floor to 0 and a client polling every 10 ms would never refill at
    all — the bucket would be permanently empty. Spending 1 token every 10 ms
    against a 0.2/10ms refill must drain steadily, not stall at zero and not
    stay full.
    """
    now = T0
    allowed = 0
    for i in range(100):
        now = T0 + i * 10
        if decode(await call(script, key, now=now))["allowed"]:
            allowed += 1

    # 20 initial + ~0.2/tick refill over 100 ticks ~= 20 + 19.8.
    assert 35 <= allowed <= 45, f"{allowed} allowed — refill is stalling or over-crediting"

    raw = await script._redis.hget(key, "t")
    assert "." in raw, f"tokens stored as {raw!r} — the fraction was lost"


async def test_stored_timestamp_is_not_mangled(script, key) -> None:
    """Lua 5.1 formats numbers with %.14g.

    A bare 13-digit epoch survives that, but the value here comes from TIME
    arithmetic and can carry a fraction, at which point %.14g starts truncating.
    string.format('%.0f') is what keeps it an integer.
    """
    await call(script, key, now=T0)
    ts = await script._redis.hget(key, "ts")
    assert "e" not in ts.lower(), f"timestamp in scientific notation: {ts!r}"
    assert "." not in ts, f"timestamp carries a fraction: {ts!r}"
    assert len(ts) == 13, f"expected a 13-digit ms epoch, got {ts!r}"


async def test_reply_contains_no_floats(script, key) -> None:
    """Lua->RESP truncates floats, so nothing may leave the script as one."""
    reply = await call(script, key, now=T0)
    assert all(isinstance(v, int) for v in reply), reply


async def test_backwards_clock_never_refunds(script, key) -> None:
    for _ in range(CAPACITY):
        await call(script, key, now=T0 + 10_000)
    r = decode(await call(script, key, now=T0))  # clock steps back
    assert not r["allowed"], "a backwards clock granted free capacity"


async def test_ttl_is_at_least_the_full_refill_time(script, key) -> None:
    """Expire sooner and the next request sees no key, assumes a full bucket,
    and a client pacing just slower than the TTL gets unlimited capacity."""
    await call(script, key, now=T0)
    pttl = await script._redis.pttl(key)
    full_refill_ms = int(CAPACITY / RATE * 1000)
    assert pttl >= full_refill_ms
    assert pttl <= full_refill_ms + PAD_MS + 50


async def test_ttl_is_refreshed_on_deny(script, key) -> None:
    """Otherwise a continuously blocked client expires into a full bucket —
    being rate limited would REWARD it."""
    for _ in range(CAPACITY):
        await call(script, key, now=T0)
    await script._redis.pexpire(key, 200)
    denied = decode(await call(script, key, now=T0))
    assert not denied["allowed"]
    assert await script._redis.pttl(key) > 500, "TTL was not refreshed on a denial"


async def test_peek_does_not_consume(script, key) -> None:
    """Inspecting a bucket must not become part of the rate limit."""
    await call(script, key, now=T0)
    before = decode(await call(script, key, now=T0, cost=0, mode="peek"))
    after = decode(await call(script, key, now=T0, cost=0, mode="peek"))
    assert before["tokens"] == after["tokens"]


async def test_peek_does_not_create_the_key(script, key) -> None:
    await call(script, key, now=T0, cost=0, mode="peek")
    assert not await script._redis.exists(key)


async def test_concurrent_consumes_allow_exactly_capacity(script, key) -> None:
    """The atomicity claim, in isolation.

    500 concurrent calls with a frozen clock — no refill is possible — must
    allow exactly `capacity`. A naive GET/compute/SET allows far more, because
    two callers read the same value and both write back one less.
    """
    results = await asyncio.gather(*[call(script, key, now=T0) for _ in range(500)])
    allowed = sum(1 for r in results if decode(r)["allowed"])
    assert allowed == CAPACITY, f"{allowed} allowed with a frozen clock; expected {CAPACITY}"


async def test_retry_after_reflects_the_actual_deficit(script, key) -> None:
    for _ in range(CAPACITY):
        await call(script, key, now=T0)
    r = decode(await call(script, key, now=T0))
    # One token at 20/s is 50 ms.
    assert 0 < r["retry_ms"] <= 100


async def test_noscript_recovery_after_script_flush(script, key, redis_test_client) -> None:
    """EVALSHA fails after a Redis restart or SCRIPT FLUSH.

    Recovery must be transparent, and EVAL both runs and re-caches, so one
    round trip restores steady state.
    """
    assert decode(await call(script, key, now=T0))["allowed"]
    await redis_test_client.script_flush()

    results = await asyncio.gather(*[call(script, key, now=T0) for _ in range(50)])
    assert all(decode(r)["capacity"] == CAPACITY for r in results)
    assert script.noscript_recoveries >= 1, "the NOSCRIPT path was never exercised"
    # One EVAL re-registers for everyone; the rest resume on EVALSHA.
    assert script.noscript_recoveries <= 2, (
        f"{script.noscript_recoveries} recoveries — concurrent callers are stampeding EVAL"
    )
