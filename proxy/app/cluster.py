"""Cross-replica breaker state, without making Redis a dependency of failover.

THE PROBLEM. Breakers are per-process (ADR-008). With N replicas, detecting a
region outage costs N x threshold failures instead of threshold failures, and N
replicas each send their own half-open probe at a region that is trying to
recover.

WHY NOT A REDIS-BACKED BREAKER. The obvious fix — keep breaker state in Redis
with a Lua CAS — puts a network round trip in the hot path of the thing whose
entire job is surviving failures, and makes a Redis outage degrade failover
itself. That is the wrong dependency direction for a resilience component.

WHAT THIS DOES INSTEAD: gossip, not consensus.

  * Every replica keeps its own local breaker and decides locally. The hot path
    never touches Redis.
  * On a state TRANSITION (open / close), the replica publishes one message.
  * Every replica subscribes and applies transitions it hears about.

So a region outage is detected once and the verdict spreads, instead of every
replica paying to learn it independently. If Redis is down, publishes and the
subscription fail quietly and every replica falls back to exactly today's
behaviour — independent local breakers. The degraded mode is the current
correct mode, which is the property that makes this safe to add.

It is eventually consistent by construction, and that is fine: a breaker is a
heuristic about a region's health, not a lock. Two replicas briefly disagreeing
costs one extra probe.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid

from redis.exceptions import RedisError

from app.logging_setup import logx

CHANNEL = "mrfp:breaker:v1"


class BreakerGossip:
    """Publishes local breaker transitions and applies remote ones."""

    def __init__(self, redis, breaker, *, node_id: str | None = None, pubsub_redis=None) -> None:
        self._redis = redis
        # SUBSCRIBE holds a connection open and waits; PUBLISH is an ordinary
        # fast command. They need opposite timeout policies, so they get
        # separate clients. Sharing one meant every subscribe expired against
        # the hot path's 50 ms read ceiling and gossip never delivered anything.
        self._pubsub_redis = pubsub_redis if pubsub_redis is not None else redis
        self._breaker = breaker
        # Identifies our own messages so we do not act on them.
        self.node_id = node_id or f"{os.uname().nodename}-{uuid.uuid4().hex[:8]}"
        self._task: asyncio.Task | None = None
        self.published = 0
        self.applied = 0

    async def publish(self, region: str, state: str, code: str = "-") -> None:
        """Announce a local transition. Best effort, never blocking."""
        if self._redis is None:
            return
        try:
            await self._redis.publish(
                CHANNEL,
                json.dumps({"node": self.node_id, "region": region, "state": state, "code": code}),
            )
            self.published += 1
        except (RedisError, TimeoutError, OSError):
            # Silent by design: gossip is an optimisation, and a failed publish
            # must never surface as a request failure.
            pass

    async def _listen(self) -> None:
        assert self._pubsub_redis is not None
        while True:
            try:
                pubsub = self._pubsub_redis.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(CHANNEL)
                logx("breaker_gossip_subscribed", node=self.node_id)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    self._apply(message.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logx(
                    "breaker_gossip_reconnecting",
                    level=logging.WARNING,
                    error_class=type(exc).__qualname__,
                )
                await asyncio.sleep(2.0)

    def _apply(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if msg.get("node") == self.node_id:
            return  # our own transition, already applied locally

        region, state = msg.get("region"), msg.get("state")
        if not region or state not in {"open", "closed"}:
            return

        # Apply the VERDICT, not the counters. Replaying another replica's
        # failure count would double-count evidence; what we want is "someone
        # who tried it says it is down".
        if state == "open":
            self._breaker.force_open(region, code=msg.get("code", "remote"))
        else:
            self._breaker.record_success(region)
        self.applied += 1
        logx("breaker_gossip_applied", region=region, state=state, from_node=msg.get("node"))

    async def start(self) -> None:
        if self._redis is None:
            return
        self._task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


def replica_count() -> int:
    """How many replicas share this deployment.

    Used to divide the in-process fallback bucket so that N replicas failing
    open do not collectively enforce N times the configured limit. An env var
    rather than service discovery: it is one number, it changes when you scale,
    and reading it wrong degrades a limit rather than breaking a request.
    """
    try:
        return max(1, int(os.getenv("MRFP_REPLICAS", "1")))
    except ValueError:
        return 1
