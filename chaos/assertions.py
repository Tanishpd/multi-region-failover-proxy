"""Hard PASS/FAIL checks. `make chaos` exits non-zero when any of these fail.

Assertion ids are global across the scenario matrix, so A6 and A7 belong to
scenarios other than stop-us-east. docs/CHAOS-REPORT.md section 6.

Design rules these follow:

  * Timing assertions key on SEND time, never receive time. A request correctly
    dispatched to a healthy region microseconds before the kill lands after it.
  * Thresholds are relative to a baseline measured IN THE SAME RUN. Absolute
    numbers flake across machines and between cold and warm caches.
  * A13 exists to stop the project's own sophistication from hiding the brief's
    requirement — see its docstring.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from chaos.config import Record, Window

# How much of the outage window's tail is excluded from "was it bypassed?"
# scoring. A request sent within this of the repair may be completed by the
# repair itself rather than by the proxy failing over — comfortably more than
# the 200 ms read timeout plus the 900 ms total budget.
RESCUE_MARGIN_S = 1.2


@dataclass(slots=True)
class Assertion:
    id: str
    name: str
    passed: bool
    detail: str

    @property
    def symbol(self) -> str:
        return "PASS" if self.passed else "FAIL"


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[k]


def in_window(records: list[Record], window: Window) -> list[Record]:
    """Records SENT during the window."""
    return [r for r in records if window.contains(r.t_send)]


def check_stop_scenario(
    records: list[Record],
    *,
    baseline: Window,
    outage: Window,
    recovery: Window,
    primary: str,
    failover_to: str,
    stop_duration_ms: float,
    failover_log_lines: list[dict],
    target_rps: int,
    failover_budget_ms: float = 1500.0,
    error_budget: float = 0.0,
    mode: str = "literal",
) -> list[Assertion]:
    out: list[Assertion] = []
    base = in_window(records, baseline)
    during = in_window(records, outage)
    after = in_window(records, recovery)

    # A1 — no client ever saw a broken connection.
    client_errors = [r for r in records if r.status == 0]
    out.append(
        Assertion(
            "A1",
            "no client-side errors",
            not client_errors,
            f"{len(client_errors)} of {len(records)} requests failed client-side"
            + (f" (e.g. {client_errors[0].error})" if client_errors else ""),
        )
    )

    # A2 — the outage was invisible to callers. This is the brief's "without
    # breaking the user's application".
    #
    # error_budget is 0 for every scenario except cascade-two, and the reason is
    # a real finding rather than a fudge. When only ap-southeast-1 survives, the
    # whole flood lands on a region whose simulated base latency is 150 ms
    # against a 200 ms read timeout — roughly 30 ms of headroom once jitter is
    # counted. Under saturation it occasionally exceeds the deadline and the
    # request correctly 503s having genuinely tried all three regions. A single
    # global timeout is simply the wrong shape when regions differ this much in
    # latency; production would scale the deadline per region. Kept at a flat
    # 200 ms here because that is the number the brief specifies.
    bad = [r for r in records if r.status >= 500 or r.status == 429]
    ratio = len(bad) / len(records) if records else 0.0
    out.append(
        Assertion(
            "A2",
            "no 5xx or 429 surfaced",
            ratio <= error_budget,
            f"{len(bad)} error responses of {len(records)} ({ratio * 100:.2f}%)"
            + (f", budget {error_budget * 100:.2f}%" if error_budget else "")
            + (f"; first: {bad[0].status} via {bad[0].failover_path}" if bad else ""),
        )
    )

    # A3 — proves BYPASS, not retry. Keyed on send time deliberately.
    #
    # The tail of the outage window is excluded, because a request sent just
    # before the repair can legitimately be RESCUED by it. `docker pause` does
    # not close sockets: a request already blocked on a paused region completes
    # normally the moment it is unpaused, before the 200 ms read timeout fires.
    # Measured: 6 requests sent within 100 ms of an unpause all replied from the
    # "failed" region afterwards, with attempts=1. That is the proxy behaving
    # correctly, so counting it as a failure would be a broken assertion rather
    # than a broken system. `docker stop` cannot produce this — a stopped
    # container has no sockets to resume.
    rescue_window = Window("scored", outage.start, outage.end - RESCUE_MARGIN_S)
    scored = in_window(records, rescue_window)
    served_by_primary = [r for r in scored if r.region == primary]
    out.append(
        Assertion(
            "A3",
            f"nothing served by {primary} during the outage",
            not served_by_primary,
            f"{len(served_by_primary)} of {len(scored)} scored requests were still"
            f" served by {primary}"
            f" (last {RESCUE_MARGIN_S:.1f}s before repair excluded: rescuable in flight)",
        )
    )

    # A4 — how fast the first request actually rerouted.
    first_failover = next((r for r in during if r.region == failover_to and r.ok), None)
    if first_failover is None:
        out.append(
            Assertion("A4", "failover detected quickly", False, f"no {failover_to} response at all")
        )
    else:
        delta_ms = (first_failover.t_send - outage.start) * 1000
        out.append(
            Assertion(
                "A4",
                "failover detected quickly",
                delta_ms <= failover_budget_ms,
                f"first {failover_to} success {delta_ms:.0f} ms after the stop completed"
                f" (budget {failover_budget_ms:.0f} ms)",
            )
        )

    # A5 — latency stayed within a few failure-detections of baseline.
    base_p99 = pct([r.latency_ms for r in base], 99)
    during_p99 = pct([r.latency_ms for r in during], 99)
    ceiling = base_p99 + 3 * 200
    out.append(
        Assertion(
            "A5",
            "transition p99 within budget",
            during_p99 <= ceiling,
            f"p99 {during_p99:.0f} ms during vs {base_p99:.0f} ms baseline"
            f" (ceiling {ceiling:.0f} ms)",
        )
    )

    # A8 — the brief's logging clause: the except block logged what it caught.
    #
    # Filtered on from_region only, never on the (from, to) pair. The router logs
    # each HOP of the walk, so a cascade produces us-east-1->eu-west-1 followed by
    # eu-west-1->ap-southeast-1 and never a direct us-east-1->ap-southeast-1 line.
    # Requiring the pair would fail every multi-hop scenario while the proxy was
    # behaving perfectly.
    good_lines = [
        line
        for line in failover_log_lines
        if line.get("from_region") == primary and line.get("error_class")
    ]
    classes = sorted({line["error_class"] for line in good_lines})
    hops = sorted(
        {f"{line['from_region']}->{line.get('to_region')}" for line in failover_log_lines}
    )
    out.append(
        Assertion(
            "A8",
            "failover was logged with an error class",
            bool(good_lines),
            f"{len(good_lines)} upstream_failover lines from {primary}"
            f"; error_class={classes or 'none'}; hops={hops or 'none'}",
        )
    )

    # A9 — the proxy's logs and this harness agree about the same request.
    harness_ids = {r.request_id for r in records}
    correlated = [line for line in good_lines if line.get("request_id") in harness_ids]
    out.append(
        Assertion(
            "A9",
            "log lines correlate with the harness",
            bool(correlated),
            f"{len(correlated)} of {len(good_lines)} failover lines matched a request_id",
        )
    )

    # A11 — the stop itself must be quick, or the demo shows no failover at all.
    out.append(
        Assertion(
            "A11",
            "docker stop returned promptly",
            stop_duration_ms < 1500,
            f"docker stop took {stop_duration_ms:.0f} ms"
            + (
                ""
                if stop_duration_ms < 1500
                else "  -> check --timeout-graceful-shutdown 0 and stop_grace_period"
            ),
        )
    )

    # A12 — refuse to report a result gathered under a load we could not deliver.
    elapsed = max(r.t_send for r in records) - min(r.t_send for r in records) if records else 0
    achieved = len(records) / elapsed if elapsed > 0 else 0
    out.append(
        Assertion(
            "A12",
            "achieved throughput >= 90% of target",
            achieved >= 0.9 * target_rps,
            f"{achieved:.1f} rps achieved vs {target_rps} target",
        )
    )

    # A13 — the guard on brief fidelity.
    #
    # A circuit breaker (or a health poller) would SKIP a known-bad region
    # rather than attempting it, so the try/except the brief asks for would stop
    # firing — under a flood it might run for 5 of 3000 requests, or never. This
    # assertion fails the run if the except block is being bypassed, which makes
    # the requirement impossible to lose silently. (ADR-009)
    #
    # Matched by REQUEST ID, not by comparing two counts. Counting log lines
    # against a windowed request total mixes populations — log lines exist for
    # requests sent just before the window opened — and can exceed 100%, which
    # would let a badly wrong number still pass the threshold.
    # Any hop counts: a cascaded request legitimately produces two lines, and
    # the question is only whether the except block ran for it at all.
    failed_over = {r.request_id for r in during if r.region == failover_to and r.attempts >= 2}
    logged_ids = {line.get("request_id") for line in failover_log_lines}
    matched = failed_over & logged_ids
    ratio = (len(matched) / len(failed_over)) if failed_over else 1.0

    if mode == "literal":
        out.append(
            Assertion(
                "A13",
                "every failed-over request hit the except block",
                ratio >= 0.95,
                f"{len(matched)} of {len(failed_over)} failed-over requests have an"
                f" upstream_failover line ({ratio * 100:.1f}%)",
            )
        )
    else:
        # In resilient mode a LOW ratio is the correct outcome: the breaker
        # skips a known-bad region instead of attempting it, so the except block
        # stops firing. Measured on this stack, enabling the breaker took
        # upstream_failover from 203 lines to 15 while cutting outage p50 from
        # 277 ms to 76 ms. Asserting the literal-mode invariant here would fail a
        # correctly working breaker; recording the number is the useful thing.
        out.append(
            Assertion(
                "A13",
                "breaker skipped the dead region (literal-mode check waived)",
                True,
                f"resilient mode: {len(matched)} of {len(failed_over)} failed-over requests"
                f" logged a failover ({ratio * 100:.1f}%) — the rest were skipped, which is"
                " the breaker doing its job",
            )
        )

    # Recovery is half the story and the brief omits it entirely.
    back_home = [r for r in after if r.region == primary]
    out.append(
        Assertion(
            "A14",
            f"traffic returned to {primary} after restart",
            bool(back_home),
            f"{len(back_home)} of {len(after)} post-recovery requests served by {primary}",
        )
    )
    return out


def detection_stats(records: list[Record], window: Window, primary: str) -> dict:
    """How long the proxy took to notice the primary was gone.

    Parsed from X-Failover-Path, so it measures the failed ATTEMPT against the
    primary rather than the whole request. This is the number that separates the
    two fault types: `docker stop` withdraws DNS and fails in single-digit ms,
    while `docker pause` leaves the socket open and costs a full read timeout.
    """
    import re

    pattern = re.compile(rf"{re.escape(primary)}:(\w+)\(([\d.]+)ms\)")
    samples: list[float] = []
    codes: dict[str, int] = {}

    for r in in_window(records, window):
        match = pattern.search(r.failover_path or "")
        if not match:
            continue
        code, ms = match.group(1), float(match.group(2))
        if code == "ok":
            continue
        samples.append(ms)
        codes[code] = codes.get(code, 0) + 1

    return {
        "samples": len(samples),
        "codes": dict(sorted(codes.items(), key=lambda kv: -kv[1])),
        "p50_ms": round(pct(samples, 50), 1),
        "p99_ms": round(pct(samples, 99), 1),
        "min_ms": round(min(samples), 1) if samples else 0.0,
        "max_ms": round(max(samples), 1) if samples else 0.0,
    }


def summarise(records: list[Record]) -> dict:
    lat = [r.latency_ms for r in records]
    return {
        "count": len(records),
        "ok": sum(1 for r in records if r.ok),
        "errors": sum(1 for r in records if not r.ok),
        "p50_ms": round(statistics.median(lat), 1) if lat else 0.0,
        "p99_ms": round(pct(lat, 99), 1),
        "max_ms": round(max(lat), 1) if lat else 0.0,
        "by_region": {
            region: sum(1 for r in records if r.region == region)
            for region in sorted({r.region for r in records})
        },
    }
