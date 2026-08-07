"""Chaos CLI. Exits non-zero when any assertion fails.

    python -m chaos.run --scenario stop-us-east

Runs on the HOST, because it needs the docker CLI and cannot live inside the
stack it is breaking.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import signal
import sys
import time
from pathlib import Path

from chaos import proxy_logs
from chaos.assertions import detection_stats, in_window, summarise
from chaos.config import ARTIFACTS, FloodConfig
from chaos.docker_ctl import DockerControl, container_is_running
from chaos.scenarios import SCENARIOS, ScenarioResult, assert_scenario
from chaos.timeline import render

PROXY_CONTAINER = "mrfp-proxy"

# Strong references to fire-and-forget tasks, so none is collected mid-flight.
_BACKGROUND: set[asyncio.Task] = set()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


async def preflight(cfg: FloodConfig) -> None:
    """Fail fast and legibly rather than producing a meaningless run.

    Every assertion presumes a working steady state; without this check, a
    stopped stack yields a wall of confusing failures instead of one clear line.
    """
    import httpx

    from chaos.config import CONTAINERS

    for container in CONTAINERS.values():
        if not await container_is_running(container):
            raise SystemExit(f"preflight: {container} is not running — run `make up` first")

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.post(
                cfg.url, json=cfg.payload, headers={"content-type": "application/json"}
            )
        except Exception as exc:
            raise SystemExit(f"preflight: the proxy is unreachable at {cfg.url} ({exc})") from exc

    if resp.status_code != 200:
        raise SystemExit(f"preflight: {cfg.url} returned {resp.status_code}, expected 200")
    if not resp.headers.get("x-served-by-region"):
        raise SystemExit("preflight: X-Served-By-Region is missing — the harness keys on it")


def write_artifacts(result: ScenarioResult, logs: list[dict], run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)

    with (run_dir / "requests.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "seq",
                "request_id",
                "t_send_rel",
                "t_recv_rel",
                "status",
                "region",
                "attempts",
                "latency_ms",
                "late_by_ms",
                "failover_path",
                "error",
            ]
        )
        for r in result.records:
            writer.writerow(
                [
                    r.seq,
                    r.request_id,
                    f"{r.t_send - result.t0:.4f}",
                    f"{r.t_recv - result.t0:.4f}",
                    r.status,
                    r.region,
                    r.attempts,
                    f"{r.latency_ms:.2f}",
                    f"{r.late_by_ms:.2f}",
                    r.failover_path,
                    r.error,
                ]
            )

    with (run_dir / "proxy-logs.jsonl").open("w") as fh:
        for line in logs:
            fh.write(json.dumps(line) + "\n")

    timeline = render(result.records, result.events, result.t0)
    (run_dir / "timeline.txt").write_text(timeline + "\n")

    summary = {
        "scenario": result.name,
        "passed": result.passed,
        "generated_at": time.time(),
        # The literal commands that ran, so the report can prove the brief's own
        # `docker stop us-east-1-endpoint` is what was actually executed.
        "commands_executed": result.docker_actions,
        "notes": result.notes,
        "windows": {
            name: {"start_rel": w.start - result.t0, "end_rel": w.end - result.t0}
            for name, w in result.windows.items()
        },
        "overall": summarise(result.records),
        "by_window": {
            name: summarise(in_window(result.records, w)) for name, w in result.windows.items()
        },
        # The number that distinguishes the fault types: how long the failed
        # attempt against the primary took before the router moved on.
        "detection": detection_stats(
            result.records, result.windows["outage"], result.notes.get("primary", "us-east-1")
        ),
        "log_event_counts": proxy_logs.event_counts(logs),
        "assertions": [
            {"id": a.id, "name": a.name, "passed": a.passed, "detail": a.detail}
            for a in result.assertions
        ],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def print_report(result: ScenarioResult, run_dir: Path) -> None:
    print()
    print(_c("1", f"  scenario: {result.name}"))
    print()
    for action in result.docker_actions:
        print(f"  executed: {_c('36', action['command'])}  ({action['duration_ms']:.0f} ms)")
    print()
    print(render(result.records, result.events, result.t0))
    print()

    for a in result.assertions:
        colour = "32" if a.passed else "31"
        print(f"  {_c(colour, a.symbol)}  {a.id:<4} {a.name:<48} {a.detail}")

    print()
    verdict = "PASS" if result.passed else "FAIL"
    print(f"  RESULT: {_c('32' if result.passed else '31', verdict)}    artifacts: {run_dir}")
    print()


async def main_async(args: argparse.Namespace) -> int:
    cfg = FloodConfig(rps=args.rps, duration_s=args.duration)
    scenario = SCENARIOS[args.scenario]

    await preflight(cfg)

    docker = DockerControl()
    loop = asyncio.get_running_loop()

    # Ctrl-C must never leave a region stopped or paused. The undo stack is
    # drained here AND in the finally block below.
    #
    # The task is held in a module-level set: a bare create_task keeps only a
    # weak reference, so the restore could be garbage collected mid-flight and
    # the interrupt would leave containers down — the exact failure this handler
    # exists to prevent.
    def _on_sigint() -> None:
        print("\n  interrupted — restoring containers...", file=sys.stderr)
        task = loop.create_task(docker.restore())
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)

    loop.add_signal_handler(signal.SIGINT, _on_sigint)

    since = time.time()
    try:
        result = await scenario(cfg, docker)
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        await docker.restore()

    logs = await proxy_logs.capture(PROXY_CONTAINER, since)
    assert_scenario(result, proxy_logs.failover_lines(logs), args.rps)

    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{result.name}"
    run_dir = ARTIFACTS / run_id
    write_artifacts(result, logs, run_dir)

    latest = ARTIFACTS / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(run_dir.name)

    print_report(result, run_dir)
    return 0 if result.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="chaos", description="Chaos scenarios for the proxy.")
    parser.add_argument("--scenario", default="stop-us-east", choices=sorted(SCENARIOS))
    parser.add_argument("--rps", type=int, default=50)
    parser.add_argument("--duration", type=float, default=16.0)
    args = parser.parse_args()

    try:
        return asyncio.run(main_async(args))
    except SystemExit as exc:
        print(f"\n  {_c('31', 'ABORT')}  {exc}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
