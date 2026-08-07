"""The scenario matrix.

Each scenario runs a flood, injects one fault at a known moment, restores, and
returns everything needed to assert and report. The two that matter most are
`stop-us-east` (the brief's own command) and `pause-us-east` (the only one that
reaches the 200 ms read timeout).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from chaos.assertions import Assertion, check_stop_scenario
from chaos.config import CONTAINERS, FloodConfig, Record, Window
from chaos.docker_ctl import DockerControl
from chaos.flood import flood


@dataclass(slots=True)
class ScenarioResult:
    name: str
    records: list[Record]
    assertions: list[Assertion]
    events: list[tuple[float, str]]
    windows: dict[str, Window]
    docker_actions: list[dict]
    t0: float
    notes: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(a.passed for a in self.assertions)


async def _flood_with_fault(
    cfg: FloodConfig,
    *,
    settle_s: float,
    fault: Callable[[], Awaitable[float]],
    outage_s: float,
    repair: Callable[[], Awaitable[None]],
) -> tuple[list[Record], list[tuple[float, str]], dict[str, Window], float, float]:
    """Run the flood in the background and inject the fault mid-flight."""
    t0 = time.monotonic()
    events: list[tuple[float, str]] = []

    flood_task = asyncio.create_task(flood(cfg))

    await asyncio.sleep(settle_s)
    baseline_end = time.monotonic()
    events.append((baseline_end, "fault injected"))

    fault_ms = await fault()
    outage_start = time.monotonic()
    events.append((outage_start, "fault complete"))

    await asyncio.sleep(outage_s)
    outage_end = time.monotonic()
    events.append((outage_end, "repairing"))

    await repair()
    recovery_start = time.monotonic()
    events.append((recovery_start, "repaired"))

    records = await flood_task
    end = time.monotonic()

    windows = {
        "baseline": Window("baseline", t0, baseline_end),
        # The outage window starts only once the stop has RETURNED, so requests
        # in flight during the stop itself are not attributed to either side.
        "outage": Window("outage", outage_start, outage_end),
        "recovery": Window("recovery", recovery_start + 1.0, end),
    }
    return records, events, windows, t0, fault_ms


async def stop_us_east(cfg: FloodConfig, docker: DockerControl) -> ScenarioResult:
    """The brief: `docker stop us-east-1-endpoint` during a flood.

    Produces a ConnectError in single-digit milliseconds — Docker's resolver
    withdraws the DNS record — so this scenario never reaches the read timeout.
    That is the point of pause_us_east.
    """
    container = CONTAINERS["us-east-1"]
    fault_ms = 0.0

    async def fault() -> float:
        nonlocal fault_ms
        action = await docker.stop(container)
        fault_ms = action.duration_ms
        return fault_ms

    async def repair() -> None:
        await docker.start(container)
        await asyncio.sleep(3.0)  # let the region come back healthy

    records, events, windows, t0, _ = await _flood_with_fault(
        cfg, settle_s=4.0, fault=fault, outage_s=4.0, repair=repair
    )
    return ScenarioResult(
        name="stop-us-east",
        records=records,
        assertions=[],
        events=events,
        windows=windows,
        docker_actions=docker.as_dict(),
        t0=t0,
        notes={"fault_duration_ms": fault_ms, "primary": "us-east-1", "failover_to": "eu-west-1"},
    )


async def pause_us_east(cfg: FloodConfig, docker: DockerControl) -> ScenarioResult:
    """`docker pause`: the only fault that proves the 200 ms timeout is live.

    Pausing freezes the process without closing its sockets. The kernel still
    completes the handshake from the listen backlog, so connect SUCCEEDS and the
    read hangs until the timeout fires. Expect a transition p50 near 200 ms,
    against single-digit ms for stop. (ADR-012)
    """
    container = CONTAINERS["us-east-1"]
    fault_ms = 0.0

    async def fault() -> float:
        nonlocal fault_ms
        action = await docker.pause(container)
        fault_ms = action.duration_ms
        return fault_ms

    async def repair() -> None:
        await docker.unpause(container)
        await asyncio.sleep(2.0)

    records, events, windows, t0, _ = await _flood_with_fault(
        cfg, settle_s=4.0, fault=fault, outage_s=4.0, repair=repair
    )
    return ScenarioResult(
        name="pause-us-east",
        records=records,
        assertions=[],
        events=events,
        windows=windows,
        docker_actions=docker.as_dict(),
        t0=t0,
        notes={"fault_duration_ms": fault_ms, "primary": "us-east-1", "failover_to": "eu-west-1"},
    )


async def cascade_two(cfg: FloodConfig, docker: DockerControl) -> ScenarioResult:
    """Stop us-east-1 AND eu-west-1: proves the router is a loop, not an if/else."""
    fault_ms = 0.0

    async def fault() -> float:
        nonlocal fault_ms
        a = await docker.stop(CONTAINERS["us-east-1"])
        b = await docker.stop(CONTAINERS["eu-west-1"])
        fault_ms = a.duration_ms + b.duration_ms
        return fault_ms

    async def repair() -> None:
        await docker.start(CONTAINERS["us-east-1"])
        await docker.start(CONTAINERS["eu-west-1"])
        await asyncio.sleep(3.0)

    records, events, windows, t0, _ = await _flood_with_fault(
        cfg, settle_s=4.0, fault=fault, outage_s=4.0, repair=repair
    )
    return ScenarioResult(
        name="cascade-two",
        records=records,
        assertions=[],
        events=events,
        windows=windows,
        docker_actions=docker.as_dict(),
        t0=t0,
        notes={
            "fault_duration_ms": fault_ms,
            "primary": "us-east-1",
            "failover_to": "ap-southeast-1",
        },
    )


async def total_blackout(cfg: FloodConfig, docker: DockerControl) -> ScenarioResult:
    """All three down: the 503 contract, and the proxy surviving it."""
    fault_ms = 0.0

    async def fault() -> float:
        nonlocal fault_ms
        total = 0.0
        for region in ("us-east-1", "eu-west-1", "ap-southeast-1"):
            action = await docker.stop(CONTAINERS[region])
            total += action.duration_ms
        fault_ms = total
        return fault_ms

    async def repair() -> None:
        for region in ("us-east-1", "eu-west-1", "ap-southeast-1"):
            await docker.start(CONTAINERS[region])
        await asyncio.sleep(4.0)

    records, events, windows, t0, _ = await _flood_with_fault(
        cfg, settle_s=4.0, fault=fault, outage_s=4.0, repair=repair
    )
    return ScenarioResult(
        name="total-blackout",
        records=records,
        assertions=[],
        events=events,
        windows=windows,
        docker_actions=docker.as_dict(),
        t0=t0,
        notes={"fault_duration_ms": fault_ms, "primary": "us-east-1", "failover_to": "none"},
    )


async def recover_us_east(cfg: FloodConfig, docker: DockerControl) -> ScenarioResult:
    """Stop the primary long enough for the breaker to open, then bring it back.

    Only meaningful in resilient mode: the point is watching circuit_opened ->
    circuit_half_open -> circuit_closed, and confirming the single-flight probe
    lets traffic return without stampeding a region that is still starting.
    """
    container = CONTAINERS["us-east-1"]
    fault_ms = 0.0

    async def fault() -> float:
        nonlocal fault_ms
        action = await docker.stop(container)
        fault_ms = action.duration_ms
        return fault_ms

    async def repair() -> None:
        await docker.start(container)
        # Long enough for the breaker's cooldown to elapse and a probe to run.
        await asyncio.sleep(8.0)

    records, events, windows, t0, _ = await _flood_with_fault(
        cfg, settle_s=4.0, fault=fault, outage_s=8.0, repair=repair
    )
    return ScenarioResult(
        name="recover-us-east",
        records=records,
        assertions=[],
        events=events,
        windows=windows,
        docker_actions=docker.as_dict(),
        t0=t0,
        notes={"fault_duration_ms": fault_ms, "primary": "us-east-1", "failover_to": "eu-west-1"},
    )


SCENARIOS: dict[str, Callable] = {
    "stop-us-east": stop_us_east,
    "pause-us-east": pause_us_east,
    "cascade-two": cascade_two,
    "total-blackout": total_blackout,
    "recover-us-east": recover_us_east,
}


def assert_scenario(
    result: ScenarioResult,
    failover_log_lines: list[dict],
    rps: int,
    mode: str = "literal",
) -> None:
    """Attach the right assertion set for the scenario in place."""
    notes = result.notes
    if result.name == "total-blackout":
        result.assertions = _blackout_assertions(result)
        return

    # Only cascade-two gets an error budget: with two regions down, the entire
    # flood lands on ap-southeast-1, whose 150 ms base latency leaves little
    # headroom under a 200 ms read timeout. See the A2 comment in assertions.py.
    error_budget = 0.01 if result.name == "cascade-two" else 0.0

    result.assertions = check_stop_scenario(
        result.records,
        baseline=result.windows["baseline"],
        outage=result.windows["outage"],
        recovery=result.windows["recovery"],
        primary=notes["primary"],
        failover_to=notes["failover_to"],
        stop_duration_ms=notes["fault_duration_ms"],
        failover_log_lines=failover_log_lines,
        target_rps=rps,
        error_budget=error_budget,
        mode=mode,
    )


def _blackout_assertions(result: ScenarioResult) -> list[Assertion]:
    """Total outage inverts the usual checks: 503 is the CORRECT answer."""
    from chaos.assertions import in_window

    during = in_window(result.records, result.windows["outage"])
    after = in_window(result.records, result.windows["recovery"])

    all_503 = during and all(r.status == 503 for r in during)
    three_attempts = during and all(r.attempts == 3 for r in during if r.status == 503)
    no_client_errors = not [r for r in result.records if r.status == 0]
    recovered = [r for r in after if r.ok]

    return [
        Assertion(
            "A15",
            "every request during the outage returned a clean 503",
            bool(all_503),
            f"{sum(1 for r in during if r.status == 503)} of {len(during)} were 503",
        ),
        Assertion(
            "A16",
            "the 503 shows all three regions were attempted",
            bool(three_attempts),
            "X-Attempts was 3 on every 503"
            if three_attempts
            else "some 503s attempted fewer than 3",
        ),
        Assertion(
            "A1",
            "no client-side errors — the proxy never dropped a connection",
            no_client_errors,
            "the proxy answered every request even with all regions dead",
        ),
        Assertion(
            "A14",
            "traffic recovered after the regions came back",
            bool(recovered),
            f"{len(recovered)} of {len(after)} post-recovery requests succeeded",
        ),
    ]
