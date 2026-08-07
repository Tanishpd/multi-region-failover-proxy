"""Circuit breaker state machine, driven by an injected clock — no sleeping."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxy"))

from app.breaker import Breaker, State  # noqa: E402

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def breaker(clock) -> Breaker:
    return Breaker(failure_threshold=3, cooldown_s=5.0, max_cooldown_s=40.0, clock=clock)


def test_starts_closed_and_allows(breaker) -> None:
    assert breaker.allow("us-east-1") == (True, "closed")


def test_opens_only_after_consecutive_failures(breaker) -> None:
    breaker.record_failure("us-east-1")
    breaker.record_failure("us-east-1")
    assert breaker.allow("us-east-1")[0], "must not open below the threshold"

    breaker.record_failure("us-east-1")
    assert breaker.allow("us-east-1") == (False, "open")


def test_a_success_resets_the_failure_streak(breaker) -> None:
    """Intermittent failures must not accumulate into an outage verdict."""
    breaker.record_failure("us-east-1")
    breaker.record_failure("us-east-1")
    breaker.record_success("us-east-1")
    breaker.record_failure("us-east-1")
    breaker.record_failure("us-east-1")
    assert breaker.allow("us-east-1")[0], "streak should have reset on success"


def test_open_becomes_half_open_after_the_cooldown(breaker, clock) -> None:
    for _ in range(3):
        breaker.record_failure("us-east-1")
    assert breaker.allow("us-east-1") == (False, "open")

    clock.advance(4.9)
    assert breaker.allow("us-east-1") == (False, "open")

    clock.advance(0.2)
    assert breaker.allow("us-east-1") == (True, "half_open")


def test_half_open_admits_exactly_one_probe(breaker, clock) -> None:
    """Otherwise every in-flight request stampedes a recovering region."""
    for _ in range(3):
        breaker.record_failure("us-east-1")
    clock.advance(6)

    assert breaker.allow("us-east-1") == (True, "half_open")
    for _ in range(10):
        assert breaker.allow("us-east-1") == (False, "half_open")


def test_successful_probe_closes_the_breaker(breaker, clock) -> None:
    for _ in range(3):
        breaker.record_failure("us-east-1")
    clock.advance(6)
    breaker.allow("us-east-1")
    breaker.record_success("us-east-1")

    assert breaker.state_name("us-east-1") == State.CLOSED.value
    assert breaker.allow("us-east-1") == (True, "closed")


def test_failed_probe_reopens_with_a_longer_cooldown(breaker, clock) -> None:
    """A region that is genuinely down must not be probed every 5s forever."""
    for _ in range(3):
        breaker.record_failure("us-east-1")
    clock.advance(6)
    breaker.allow("us-east-1")
    breaker.record_failure("us-east-1")  # probe failed

    assert breaker.describe("us-east-1")["cooldown_s"] == 10.0
    clock.advance(6)
    assert breaker.allow("us-east-1") == (False, "open"), "cooldown should have doubled"
    clock.advance(5)
    assert breaker.allow("us-east-1") == (True, "half_open")


def test_cooldown_is_capped(breaker, clock) -> None:
    for _ in range(3):
        breaker.record_failure("us-east-1")
    for _ in range(10):
        clock.advance(200)
        breaker.allow("us-east-1")
        breaker.record_failure("us-east-1")
    assert breaker.describe("us-east-1")["cooldown_s"] == 40.0


def test_closing_resets_the_backoff(breaker, clock) -> None:
    for _ in range(3):
        breaker.record_failure("us-east-1")
    clock.advance(6)
    breaker.allow("us-east-1")
    breaker.record_failure("us-east-1")  # cooldown -> 10
    clock.advance(11)
    breaker.allow("us-east-1")
    breaker.record_success("us-east-1")
    assert breaker.describe("us-east-1")["cooldown_s"] == 5.0


def test_regions_are_independent(breaker) -> None:
    for _ in range(3):
        breaker.record_failure("us-east-1")
    assert breaker.allow("us-east-1")[0] is False
    assert breaker.allow("eu-west-1")[0] is True


def test_the_same_class_guards_redis(breaker) -> None:
    """One breaker, two uses: regions and Redis-as-a-dependency. (ADR-008)"""
    for _ in range(3):
        breaker.record_failure("redis", code="ConnectionError")
    assert breaker.allow("redis") == (False, "open")
    assert breaker.describe("redis")["last_code"] == "ConnectionError"


def test_on_open_without_a_running_loop_does_not_raise(clock) -> None:
    """The purge hook must not explode when there is no event loop.

    It is scheduled with create_task, which raises RuntimeError outside a loop —
    and a breaker that cannot record a failure during shutdown would turn a
    tidy exit into a traceback.
    """

    async def purge(_region: str) -> None:  # pragma: no cover - never awaited here
        pass

    b = Breaker(failure_threshold=1, clock=clock, on_open=purge)
    b.record_failure("us-east-1")  # must not raise
    assert b.state_name("us-east-1") == "open"


def test_expired_probe_token_is_reclaimed(breaker, clock) -> None:
    """A probe that never reports back must not wedge the region forever.

    record_success/record_failure are the only things that release the token,
    and neither runs if the request task is cancelled mid-probe. Without a
    deadline, one cancelled probe pins a RECOVERED region out of rotation until
    the process restarts.
    """
    for _ in range(3):
        breaker.record_failure("us-east-1")
    clock.advance(6)

    assert breaker.allow("us-east-1") == (True, "half_open")
    # The probe vanishes — cancelled, or the caller forgot to record an outcome.
    assert breaker.allow("us-east-1") == (False, "half_open")

    clock.advance(breaker.probe_timeout_s + 0.1)
    assert breaker.allow("us-east-1") == (True, "half_open"), "token was never reclaimed"


def test_a_slow_probe_is_not_prematurely_duplicated(breaker, clock) -> None:
    """The deadline must exceed a legitimately slow request, or two probes race."""
    for _ in range(3):
        breaker.record_failure("us-east-1")
    clock.advance(6)
    breaker.allow("us-east-1")

    clock.advance(breaker.probe_timeout_s - 0.1)
    assert breaker.allow("us-east-1") == (False, "half_open")
