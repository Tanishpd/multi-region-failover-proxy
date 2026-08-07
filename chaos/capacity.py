"""Measure the throughput knee, and publish it as a generated number.

The point is not a big number. It is that the capacity figure in the docs came
from a run rather than from arithmetic, and that re-running it is one command.

Two ceilings bound this system and they are different in kind:

  CPU        the per-request cost of the proxy itself. Measured here.
  CONCURRENCY Little's Law: during a total blackout every request costs the full
             failover budget, so throughput cannot exceed
             max_inflight / total_budget regardless of how fast the CPU is.
             That one is arithmetic and needs no benchmark.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys
import time

import httpx

from chaos.config import ROOT

SLO_DOC = ROOT / "docs" / "SLO.md"
URL = "http://127.0.0.1:8000/v1/infer"


async def _measure(rps: int, seconds: float, token: str) -> dict:
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    interval = 1.0 / rps
    tasks: set[asyncio.Task] = set()

    async with httpx.AsyncClient(
        timeout=10.0,
        transport=httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=rps * 4), retries=0),
    ) as client:

        async def one() -> None:
            t0 = time.perf_counter()
            try:
                r = await client.post(
                    URL,
                    json={"prompt": "capacity", "max_tokens": 4},
                    headers={"content-type": "application/json", "x-chaos-bypass": token},
                )
                code = r.status_code
            except Exception:
                code = 0
            latencies.append((time.perf_counter() - t0) * 1000)
            statuses[code] = statuses.get(code, 0) + 1

        start = time.monotonic()
        nxt = start
        while time.monotonic() - start < seconds:
            now = time.monotonic()
            if nxt > now:
                await asyncio.sleep(nxt - now)
            t = asyncio.create_task(one())
            tasks.add(t)
            t.add_done_callback(tasks.discard)
            nxt += interval
        if tasks:
            await asyncio.wait(tasks, timeout=15)

    ordered = sorted(latencies)

    def p(q: float) -> float:
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))]

    return {
        "target_rps": rps,
        "achieved_rps": round(len(latencies) / seconds, 1),
        "p50_ms": round(statistics.median(ordered), 1) if ordered else 0.0,
        "p99_ms": round(p(0.99), 1),
        "ok": statuses.get(200, 0),
        "shed": statuses.get(503, 0),
        "errors": sum(v for k, v in statuses.items() if k not in (200,)),
    }


async def main() -> int:
    import os

    token = os.getenv("MRFP_CHAOS_TOKEN", "")
    if not token:
        print("capacity: run via `make capacity` (it mints a bypass token)", file=sys.stderr)
        return 2

    steps = [50, 100, 200, 400, 800, 1200]
    rows = []
    print(f"  {'target':>7} {'achieved':>9} {'p50':>8} {'p99':>9} {'shed':>6}")
    for rps in steps:
        row = await _measure(rps, 5.0, token)
        rows.append(row)
        print(
            f"  {row['target_rps']:>7} {row['achieved_rps']:>9} "
            f"{row['p50_ms']:>7}ms {row['p99_ms']:>8}ms {row['shed']:>6}"
        )
        # The knee: sustained rate falls well short of target, or the admission
        # gate begins shedding. Either means we found the ceiling.
        if row["achieved_rps"] < rps * 0.85 or row["shed"] > 0:
            break
        await asyncio.sleep(1)

    # The knee is the highest rate that still SERVES WELL, not the highest
    # number the generator managed to push. Picking max(achieved_rps) reported
    # 400 rps on this machine while p99 was already 1141 ms — a capacity figure
    # nobody could actually run at.
    baseline_p99 = rows[0]["p99_ms"] or 1.0
    healthy = [r for r in rows if r["p99_ms"] <= baseline_p99 * 3 and r["shed"] == 0]
    best = max(healthy, key=lambda r: r["achieved_rps"]) if healthy else rows[0]
    degraded = [r for r in rows if r not in healthy]
    block = [
        "",
        f"| Measured | {time.strftime('%Y-%m-%d')} |",
        "|---|---|",
        f"| Knee (sustained, p99 healthy) | **{best['achieved_rps']} rps** |",
        f"| p50 at the knee | {best['p50_ms']} ms |",
        f"| p99 at the knee | {best['p99_ms']} ms |",
        f"| First degraded step | {degraded[0]['target_rps'] if degraded else 'none'} rps"
        f" (p99 {degraded[0]['p99_ms'] if degraded else 0} ms) |",
        "",
        "_The knee is the highest rate whose p99 stayed within 3x the lightest"
        " step's, not the highest rate the generator could push._",
        "",
        "| target rps | achieved | p50 | p99 | shed |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        block.append(
            f"| {r['target_rps']} | {r['achieved_rps']} | {r['p50_ms']} ms | "
            f"{r['p99_ms']} ms | {r['shed']} |"
        )
    block.append("")

    text = SLO_DOC.read_text()
    text = re.sub(
        r"(<!-- AUTOGEN:BEGIN:capacity -->)(.*?)(<!-- AUTOGEN:END -->)",
        lambda m: m.group(1) + "\n".join(block) + m.group(3),
        text,
        flags=re.DOTALL,
    )
    SLO_DOC.write_text(text)
    print(f"\n  knee (p99 healthy): {best['achieved_rps']} rps  p99={best['p99_ms']}ms")
    if degraded:
        print(
            f"  first degraded step: {degraded[0]['target_rps']} rps "
            f"(p99 {degraded[0]['p99_ms']}ms, shed {degraded[0]['shed']})"
        )
    print("  -> docs/SLO.md updated")
    (ROOT / "artifacts" / "capacity.json").write_text(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
