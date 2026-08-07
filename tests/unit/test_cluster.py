"""Cross-replica breaker gossip, and the replica-aware fallback bucket.

The property that makes gossip safe to add: when Redis is unavailable, every
replica falls back to exactly the behaviour it had before gossip existed —
independent local breakers. The degraded mode is the previously-correct mode.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxy"))

from app.breaker import Breaker  # noqa: E402
from app.cluster import BreakerGossip, replica_count  # noqa: E402
from app.ratelimit.local_fallback import LocalBucket  # noqa: E402

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_a_remote_open_verdict_is_applied_without_local_failures() -> None:
    """The whole point: detect once, not once per replica."""
    breaker = Breaker(failure_threshold=5, clock=Clock())
    gossip = BreakerGossip(None, breaker, node_id="me")

    assert breaker.state_name("us-east-1") == "closed"
    gossip._apply(json.dumps({"node": "other", "region": "us-east-1", "state": "open"}))
    assert breaker.state_name("us-east-1") == "open"
    # The VERDICT was applied, not the counters — replaying another replica's
    # failure count would double-count the same evidence.
    assert breaker.describe("us-east-1")["consecutive_failures"] == 0


def test_a_remote_close_verdict_reopens_the_region() -> None:
    breaker = Breaker(failure_threshold=1, clock=Clock())
    gossip = BreakerGossip(None, breaker, node_id="me")
    breaker.record_failure("us-east-1")
    assert breaker.state_name("us-east-1") == "open"

    gossip._apply(json.dumps({"node": "other", "region": "us-east-1", "state": "closed"}))
    assert breaker.state_name("us-east-1") == "closed"


def test_a_replica_ignores_its_own_message() -> None:
    """Otherwise replicas echo each other's transitions forever."""
    breaker = Breaker(failure_threshold=5, clock=Clock())
    gossip = BreakerGossip(None, breaker, node_id="me")

    gossip._apply(json.dumps({"node": "me", "region": "us-east-1", "state": "open"}))
    assert breaker.state_name("us-east-1") == "closed"
    assert gossip.applied == 0


def test_malformed_gossip_is_ignored_rather_than_fatal() -> None:
    """A bad message on a shared channel must not take a replica down."""
    breaker = Breaker(failure_threshold=5, clock=Clock())
    gossip = BreakerGossip(None, breaker, node_id="me")

    for bad in (b"not json", None, json.dumps({"node": "o"}), json.dumps({"region": "x"})):
        gossip._apply(bad)
    assert breaker.state_name("us-east-1") == "closed"


def test_a_locally_applied_remote_verdict_is_not_re_gossiped() -> None:
    """force_open must not re-announce, or two replicas ping-pong indefinitely."""
    announced: list[tuple] = []
    breaker = Breaker(failure_threshold=5, clock=Clock())
    breaker.on_transition = lambda *args: announced.append(args)

    breaker.force_open("us-east-1", code="remote")
    assert breaker.state_name("us-east-1") == "open"
    assert announced == [], "a remote verdict was echoed back onto the channel"


def test_a_local_transition_IS_announced() -> None:
    announced: list[tuple] = []
    breaker = Breaker(failure_threshold=2, clock=Clock())
    breaker.on_transition = lambda *args: announced.append(args)

    breaker.record_failure("us-east-1", code="connect_error")
    breaker.record_failure("us-east-1", code="connect_error")
    assert announced == [("us-east-1", "open", "connect_error")]


async def test_publishing_without_redis_is_a_no_op() -> None:
    """Redis down must degrade to independent local breakers, not an error."""
    breaker = Breaker(clock=Clock())
    gossip = BreakerGossip(None, breaker)
    await gossip.publish("us-east-1", "open")  # must not raise
    assert gossip.published == 0
    await gossip.start()  # must not raise
    await gossip.stop()


# --- the fallback bucket ----------------------------------------------------


def test_the_fallback_bucket_is_divided_across_replicas() -> None:
    """With Redis down, N replicas would otherwise enforce N x the limit.

    Each replica enforcing capacity/N keeps the fleet total near the configured
    value. Approximate, since traffic is not evenly balanced — but far closer
    than N x, and the exact answer needs the shared store that is by definition
    unavailable on this path.
    """
    solo = LocalBucket(capacity=30, refill_per_sec=30, replicas=1)
    allowed_solo = sum(1 for _ in range(60) if solo.consume("ip", now_ms=1000)[0])

    shared = LocalBucket(capacity=30, refill_per_sec=30, replicas=3)
    allowed_shared = sum(1 for _ in range(60) if shared.consume("ip", now_ms=1000)[0])

    assert allowed_solo == 30
    assert allowed_shared == 10, "one replica of three should enforce a third"


def test_replica_count_defaults_to_one_and_survives_garbage() -> None:
    import os

    saved = os.environ.get("MRFP_REPLICAS")
    try:
        os.environ.pop("MRFP_REPLICAS", None)
        assert replica_count() == 1
        os.environ["MRFP_REPLICAS"] = "4"
        assert replica_count() == 4
        # A misread here should degrade a limit, never break a request.
        os.environ["MRFP_REPLICAS"] = "not-a-number"
        assert replica_count() == 1
        os.environ["MRFP_REPLICAS"] = "0"
        assert replica_count() == 1
    finally:
        os.environ.pop("MRFP_REPLICAS", None)
        if saved is not None:
            os.environ["MRFP_REPLICAS"] = saved
