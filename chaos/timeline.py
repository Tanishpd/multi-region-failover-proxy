"""The per-second ASCII timeline.

Deliberately ASCII rather than a rendered chart: it pastes into markdown, into a
terminal transcript, and into the generated report unchanged, and it is
reproducible. A dashboard screenshot is none of those things. (ADR-010)
"""

from __future__ import annotations

from chaos.assertions import pct
from chaos.config import REGIONS, Record

_BLOCKS = "▁▂▃▄▅▆▇█"


def render(records: list[Record], events: list[tuple[float, str]], t0: float) -> str:
    if not records:
        return "(no requests recorded)"

    buckets: dict[int, list[Record]] = {}
    for r in records:
        buckets.setdefault(int(r.t_send - t0), []).append(r)

    span = max(buckets) + 1
    peak = max((len(v) for v in buckets.values()), default=1)

    marks: dict[int, list[str]] = {}
    for at, label in events:
        marks.setdefault(int(at - t0), []).append(label)

    header = f"{'sec':>4} | {'us-east':>7} | {'eu-west':>7} | {'ap-se':>6} | {'err':>4} | {'p99ms':>6} | traffic"
    lines = [header, "-" * len(header)]

    for sec in range(span):
        rows = buckets.get(sec, [])
        counts = {region: sum(1 for r in rows if r.region == region) for region in REGIONS}
        errors = sum(1 for r in rows if not r.ok)
        p99 = pct([r.latency_ms for r in rows], 99)

        if rows:
            level = min(len(_BLOCKS) - 1, int(len(rows) / peak * (len(_BLOCKS) - 1)))
            bar = _BLOCKS[level] * max(1, round(len(rows) / peak * 12))
        else:
            bar = ""

        line = (
            f"{sec:>4} | {counts['us-east-1']:>7} | {counts['eu-west-1']:>7} | "
            f"{counts['ap-southeast-1']:>6} | {errors:>4} | {p99:>6.0f} | {bar}"
        )
        if sec in marks:
            line += "   <- " + "; ".join(marks[sec])
        lines.append(line)

    return "\n".join(lines)
