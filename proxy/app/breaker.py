"""One in-process circuit breaker, used for regions AND for Redis.

    CLOSED --k consecutive failures--> OPEN --cooldown--> HALF_OPEN
    HALF_OPEN --probe ok--> CLOSED
    HALF_OPEN --probe fails--> OPEN (with a longer cooldown)

WHY IT EXISTS. Without one, a dead primary is a permanent tax: every request
tries us-east-1, waits for it to fail, and only then moves on. Measured on this
stack, that is ~4 ms per request for a stopped region and ~200 ms for a paused
one, for the entire outage.

WHY IT IS OFF BY DEFAULT. It also removes the brief's requirement from the demo.
Once open, requests are SKIPPED rather than attempted, so the try/except stops
firing — under a 50 rps flood it would run for ~5 of 800 requests instead of all
200. MRFP_MODE=literal is the default so the requirement is genuinely exercised,
and `region_skipped_breaker_open` is a distinct log event from
`upstream_failover` so "caught it" and "skipped it" can never be conflated in a
report. (ADR-009)

IN-PROCESS, not Redis-backed. The whole argument for distributed breaker state
is `uvicorn --workers N`, and one worker is pinned anyway so prometheus-client
reports complete counters. It also avoids making Redis a dependency of the
resilience path. (ADR-008)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from app.logging_setup import logx

# Strong references to fire-and-forget tasks. A bare create_task keeps only a
# weak reference, so a pool purge could be garbage collected mid-flight and the
# stale-keepalive problem it exists to fix would silently persist.
_BACKGROUND: set[asyncio.Task] = set()


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class _Entry:
    state: State = State.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    cooldown_s: float = 5.0
    probe_inflight: bool = False
    # When the in-flight probe was admitted. A probe that never reports back —
    # because the task was cancelled, or the caller forgot to record an outcome
    # — would otherwise hold the single token forever, wedging a recovered
    # region out of rotation until the process restarts. This is what makes the
    # token reclaimable.
    probe_started_at: float = 0.0
    last_code: str = "-"
    transitions: int = 0


@dataclass
class Breaker:
    failure_threshold: int = 5
    cooldown_s: float = 5.0
    max_cooldown_s: float = 60.0
    on_open: Callable[[str], Awaitable[None]] | None = None
    # Called on every LOCAL transition so it can be gossiped to other replicas.
    # Not called for transitions applied FROM gossip, or replicas would echo
    # each other forever.
    on_transition: Callable[[str, str, str], None] | None = None
    clock: Callable[[], float] = time.monotonic
    # How long a half-open probe may stay outstanding before its token is
    # reclaimed. Must exceed the total request budget, or a legitimately slow
    # probe gets a second one admitted alongside it.
    probe_timeout_s: float = 5.0
    _entries: dict[str, _Entry] = field(default_factory=dict)

    def _entry(self, key: str) -> _Entry:
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry(cooldown_s=self.cooldown_s)
            self._entries[key] = entry
        return entry

    def allow(self, key: str) -> tuple[bool, str]:
        """May we send a request to `key`? Returns (allowed, state name)."""
        entry = self._entry(key)

        if entry.state is State.CLOSED:
            return True, State.CLOSED.value

        if entry.state is State.OPEN:
            if self.clock() < entry.opened_at + entry.cooldown_s:
                return False, State.OPEN.value
            entry.state = State.HALF_OPEN
            entry.probe_inflight = False
            logx("circuit_half_open", region=key, cooldown_s=entry.cooldown_s)

        # HALF_OPEN admits exactly ONE probe. Letting every in-flight request
        # through would stampede a region that is still recovering and knock it
        # straight back down.
        #
        # But the token must be reclaimable. `record_success`/`record_failure`
        # are the only things that release it, and neither runs if the request
        # task is cancelled mid-probe — so a single cancelled probe would pin
        # the region OUT of rotation permanently, even after it recovered. The
        # deadline turns that from a wedge into a delay.
        now = self.clock()
        if entry.probe_inflight:
            if now - entry.probe_started_at < self.probe_timeout_s:
                return False, State.HALF_OPEN.value
            logx(
                "circuit_probe_expired",
                level=logging.WARNING,
                region=key,
                after_s=round(now - entry.probe_started_at, 1),
            )
        entry.probe_inflight = True
        entry.probe_started_at = now
        return True, State.HALF_OPEN.value

    def force_open(self, key: str, *, code: str = "remote") -> None:
        """Open on another replica's evidence, without local failures.

        Applies the VERDICT rather than the counters: replaying a remote
        replica's failure count would double-count the same evidence.
        """
        entry = self._entry(key)
        if entry.state is State.OPEN:
            return
        self._open(key, entry, code, gossip=False)

    def record_success(self, key: str) -> None:
        entry = self._entry(key)
        entry.consecutive_failures = 0
        entry.probe_inflight = False
        if entry.state is not State.CLOSED:
            entry.state = State.CLOSED
            entry.cooldown_s = self.cooldown_s  # reset the backoff
            entry.transitions += 1
            logx("circuit_closed", region=key)
            if self.on_transition is not None:
                self.on_transition(key, "closed", "-")

    def record_failure(self, key: str, *, code: str = "-") -> None:
        entry = self._entry(key)
        entry.last_code = code
        entry.probe_inflight = False

        if entry.state is State.HALF_OPEN:
            # The probe failed: reopen with a longer cooldown so a region that
            # is genuinely down is not probed every few seconds forever.
            entry.cooldown_s = min(entry.cooldown_s * 2, self.max_cooldown_s)
            self._open(key, entry, code)
            return

        entry.consecutive_failures += 1
        if entry.state is State.CLOSED and entry.consecutive_failures >= self.failure_threshold:
            self._open(key, entry, code)

    def _open(self, key: str, entry: _Entry, code: str, *, gossip: bool = True) -> None:
        entry.state = State.OPEN
        entry.opened_at = self.clock()
        entry.transitions += 1
        logx(
            "circuit_opened",
            level=logging.WARNING,
            region=key,
            code=code,
            failures=entry.consecutive_failures,
            cooldown_s=entry.cooldown_s,
        )

        if gossip and self.on_transition is not None:
            self.on_transition(key, "open", code)

        if self.on_open is None:
            return
        # Opening also purges that region's connection pool: stale keep-alive
        # sockets to a restarted container are exactly what produce
        # RemoteProtocolError later. This MUST go through create_task with a
        # strong reference — a coroutine returned from a synchronous callback is
        # never awaited, so the purge would silently never happen.
        try:
            task = asyncio.get_running_loop().create_task(self.on_open(key))
        except RuntimeError:  # pragma: no cover - no loop (tests, shutdown)
            return
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)

    def state_name(self, key: str) -> str:
        return self._entry(key).state.value

    def describe(self, key: str) -> dict[str, object]:
        entry = self._entry(key)
        return {
            "state": entry.state.value,
            "consecutive_failures": entry.consecutive_failures,
            "cooldown_s": entry.cooldown_s,
            "last_code": entry.last_code,
            "transitions": entry.transitions,
        }
