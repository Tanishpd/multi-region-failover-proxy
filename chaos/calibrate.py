"""Derive the timeouts from measurement instead of guessing them.

THE PROBLEM THIS EXISTS FOR. `MRFP_READ_TIMEOUT_MS=200` is the brief's number
and is defensible because the brief says so. The prod profile's 30000 was not:
nobody measured anything to arrive at it. Worse, nothing in this repo COULD have
justified it, because the mock region slept for a constant regardless of how
many tokens were requested — so the measured p99 was the same for an 8-token
request as for a 512-token one, and no amount of measuring a constant tells you
what a generation costs.

WHAT THIS DOES. Sweeps `max_tokens` against each region directly (host ports,
not through the proxy — the read timeout bounds ONE region attempt, so that is
what has to be measured), fits latency to `ttft + slope x tokens`, and derives:

    read_timeout_ms    p99 at the largest supported request, x HEADROOM
    total_budget_ms    what that read timeout implies for the failover walk
    stream_inter_chunk_timeout_s   from the measured inter-token gap, x HEADROOM

Then it CHECKS the running configuration against those numbers and exits
non-zero if a configured timeout is below what was measured. That is the part
that makes it a gate rather than a report.

WHAT IT HONESTLY DOES NOT DO. It measures whatever is behind the regions. Run
against the shipped mock, it produces numbers describing the mock, and labels
them `backend=mock`. The regions grow a real-upstream passthrough
(`MRFP_UPSTREAM_BASE_URL`) precisely so this same command can be pointed at a
real model and produce numbers worth deploying. A tool that measures the mock
is not evidence about production; a tool that can measure production is.

Measured at low concurrency on purpose: this is SERVICE latency, the thing a
per-attempt timeout must accommodate. Latency under load is a different
question, and `chaos/capacity.py` answers it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]

# Token counts to sweep. Spread wide enough that the linear fit has something to
# fit; 512 is the region's declared ceiling (`max_tokens: le=512`), and the
# timeout has to cover the worst request a caller is ALLOWED to make, not the
# average one they happen to send.
TOKEN_STEPS = (8, 32, 128, 512)

# The largest request the API will accept (`max_tokens: Field(le=512)`). The
# timeout has to cover the worst request a caller is ALLOWED to make; anything
# less is a 503 waiting for someone to send a legal request.
SCHEMA_MAX_TOKENS = 512

# Multiply the measured p99 by this to get a configured timeout. A timeout set
# at exactly p99 fails 1% of healthy requests by construction; the headroom
# absorbs GC pauses, a noisy neighbour, and the drift between the day you
# measure and the day it matters.
HEADROOM = 2.0

REGIONS = {
    "us-east-1": 9001,
    "eu-west-1": 9002,
    "ap-southeast-1": 9003,
}


def p(values: list[float], q: float) -> float:
    """Nearest-rank percentile. n is small here, so interpolation would invent precision."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * len(ordered) + 0.5)) - 1))
    return ordered[index]


@dataclass(slots=True)
class Step:
    max_tokens: int
    samples: list[float] = field(default_factory=list)

    @property
    def p50(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    @property
    def p99(self) -> float:
        return p(self.samples, 0.99)


@dataclass(slots=True)
class RegionProfile:
    region: str
    backend: str
    model: str
    steps: list[Step]
    stream_gaps_ms: list[float] = field(default_factory=list)

    @property
    def slope_ms_per_token(self) -> float:
        """Least-squares slope of p50 against max_tokens.

        The slope is the per-token cost; the intercept is time-to-first-token.
        A flat slope means the backend does not scale with the work asked of
        it, which for a real model would mean the measurement is wrong.
        """
        xs = [float(s.max_tokens) for s in self.steps]
        ys = [s.p50 for s in self.steps]
        if len(xs) < 2:
            return 0.0
        mx, my = statistics.mean(xs), statistics.mean(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        return (
            sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom if denom else 0.0
        )

    @property
    def ttft_ms(self) -> float:
        xs = [float(s.max_tokens) for s in self.steps]
        ys = [s.p50 for s in self.steps]
        return statistics.mean(ys) - self.slope_ms_per_token * statistics.mean(xs)

    @property
    def worst_p99(self) -> float:
        return max((s.p99 for s in self.steps), default=0.0)


async def probe_region(
    client: httpx.AsyncClient, region: str, port: int, samples: int
) -> RegionProfile:
    base = f"http://127.0.0.1:{port}"

    meta = (await client.get(f"{base}/")).json()
    profile = RegionProfile(
        region=region,
        backend=meta.get("backend", "unknown"),
        model=meta.get("model", "unknown"),
        steps=[],
    )

    for tokens in TOKEN_STEPS:
        step = Step(max_tokens=tokens)
        # One warm-up, discarded: the first request pays connection setup, which
        # a per-attempt read timeout on a warm pool never sees.
        await client.post(f"{base}/v1/infer", json={"prompt": "calibrate", "max_tokens": tokens})
        for _ in range(samples):
            t0 = time.perf_counter()
            response = await client.post(
                f"{base}/v1/infer", json={"prompt": "calibrate", "max_tokens": tokens}
            )
            elapsed = (time.perf_counter() - t0) * 1000
            if response.status_code == 200:
                step.samples.append(elapsed)
        profile.steps.append(step)

    profile.stream_gaps_ms = await probe_stream_gaps(client, base)
    return profile


async def probe_stream_gaps(client: httpx.AsyncClient, base: str) -> list[float]:
    """Measure the gap BETWEEN streamed chunks.

    This is what `stream_inter_chunk_timeout_s` bounds — not the total, which is
    why it has to be measured separately. A stream that takes 60s in total but
    never pauses more than 40ms is healthy; one that stalls for 30s after two
    tokens is not, and only the gap distinguishes them.
    """
    gaps: list[float] = []
    try:
        async with client.stream(
            "POST",
            f"{base}/v1/infer",
            json={"prompt": "calibrate", "max_tokens": 128, "stream": True},
        ) as response:
            if response.status_code != 200:
                return gaps
            last = time.perf_counter()
            async for _ in response.aiter_bytes():
                now = time.perf_counter()
                gaps.append((now - last) * 1000)
                last = now
    except httpx.HTTPError:
        return gaps
    return gaps[1:]  # drop the first: it is TTFT, not an inter-chunk gap


def serviceable_tokens(profile: RegionProfile, read_timeout_ms: int) -> int:
    """How large a request this region can answer inside the read timeout.

    THE FAILURE THIS EXPOSES. A read timeout is usually reasoned about as
    "how long before I give up on a sick region". It is also, silently, a
    CAPABILITY CEILING: any request whose honest service time exceeds it can
    never be served by that region, however healthy the region is.

    Measured on this stack at the brief's 200 ms: ap-southeast-1 answers 8
    tokens in 183 ms and 512 tokens in 310 ms, so a 512-token request that
    fails over to it returns 503 — from a region that was working perfectly.
    Invisible until region latency was made to scale with the work requested,
    because before that every request cost the same. (ADR-028)
    """
    if profile.slope_ms_per_token <= 0:
        return 10**9  # latency does not scale with tokens; nothing to cap
    return max(0, int((read_timeout_ms - profile.ttft_ms) / profile.slope_ms_per_token))


def derive(profiles: list[RegionProfile], attempts: int, connect_ms: int) -> dict:
    """Turn the measurements into the settings they imply."""
    worst = max((prof.worst_p99 for prof in profiles), default=0.0)
    read_timeout_ms = int(worst * HEADROOM) + 1

    all_gaps = [g for prof in profiles for g in prof.stream_gaps_ms]
    if all_gaps:
        gap_p99 = p(all_gaps, 0.99)
        # Floored at 1s: below that, ordinary scheduler jitter on a loaded host
        # starts killing healthy streams, and the failure it exists to catch —
        # a region that has stopped producing — is never a sub-second event.
        inter_chunk_s = max(1.0, round(gap_p99 * HEADROOM / 1000.0, 2))
    else:
        # NO SAMPLES. Say so; do not derive.
        #
        # The first version applied the 1s floor unconditionally, so a run that
        # collected zero chunks still printed a confident
        # `MRFP_STREAM_INTER_CHUNK_TIMEOUT_S = 1.0` — a number with no
        # measurement behind it, produced by the very tool built to stop numbers
        # having no measurement behind them. It surfaced the moment the regions
        # were pointed at a real backend, whose passthrough did not stream.
        gap_p99 = None
        inter_chunk_s = None

    return {
        "headroom": HEADROOM,
        "read_timeout_ms": read_timeout_ms,
        "total_budget_ms": attempts * (connect_ms + read_timeout_ms),
        "stream_inter_chunk_timeout_s": inter_chunk_s,
        "measured_worst_p99_ms": round(worst, 2),
        "measured_stream_gap_p99_ms": round(gap_p99, 2) if gap_p99 is not None else None,
        "stream_samples": len(all_gaps),
    }


def check(derived: dict, configured: dict) -> list[str]:
    """Fail on any configured timeout the measurement does not support."""
    problems: list[str] = []

    for key, unit in (
        ("read_timeout_ms", "ms"),
        ("total_budget_ms", "ms"),
        ("stream_inter_chunk_timeout_s", "s"),
    ):
        want, have = derived[key], configured.get(key)
        # `want is None` means nothing was measured for this setting. Gating on
        # an unmeasured requirement is how the tool would start inventing the
        # numbers it exists to replace.
        if have is None or want is None:
            continue
        if have < want:
            problems.append(
                f"{key}={have}{unit} is below the measured requirement {want}{unit} "
                f"({HEADROOM}x the measured p99). Healthy requests will time out."
            )

    # The ceiling, not the floor. A read timeout that no region can serve the
    # largest permitted request inside is a 503 generator, and it fails only
    # for the biggest requests — the ones least likely to be in a smoke test.
    for entry in derived.get("region_ceilings", []):
        if entry["serviceable_max_tokens"] < entry["schema_max_tokens"]:
            problems.append(
                f"{entry['region']} can serve at most {entry['serviceable_max_tokens']} tokens "
                f"inside read_timeout_ms={configured.get('read_timeout_ms')}, but the API accepts "
                f"up to {entry['schema_max_tokens']}. Requests above that get a 503 from a "
                f"healthy region."
            )
    return problems


async def run(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=5.0)) as client:
        profiles = []
        for region, port in REGIONS.items():
            print(f"  probing {region} on :{port} ...", flush=True)
            profiles.append(await probe_region(client, region, port, args.samples))

    derived = derive(profiles, args.attempts, args.connect_ms)

    backends = {prof.backend for prof in profiles}
    report = {
        # Stamped first and deliberately prominent. Numbers derived from the
        # mock describe the mock.
        "backend": "mixed" if len(backends) > 1 else backends.pop(),
        "models": sorted({prof.model for prof in profiles}),
        "samples_per_step": args.samples,
        "token_steps": list(TOKEN_STEPS),
        "regions": [
            {
                "region": prof.region,
                "ttft_ms": round(prof.ttft_ms, 2),
                "ms_per_token": round(prof.slope_ms_per_token, 4),
                "steps": [
                    {
                        "max_tokens": s.max_tokens,
                        "p50_ms": round(s.p50, 2),
                        "p99_ms": round(s.p99, 2),
                        "n": len(s.samples),
                    }
                    for s in prof.steps
                ],
                "stream_gap_p50_ms": round(statistics.median(prof.stream_gaps_ms), 2)
                if prof.stream_gaps_ms
                else 0.0,
                # What this region can actually serve at the SHIPPED default
                # read timeout. Reported on every run, because the cliff is
                # silent otherwise: it only fires for the largest requests.
                "serviceable_max_tokens_at_default": min(
                    serviceable_tokens(prof, args.ceiling_at_ms), SCHEMA_MAX_TOKENS
                ),
            }
            for prof in profiles
        ],
        "ceiling_at_ms": args.ceiling_at_ms,
        "schema_max_tokens": SCHEMA_MAX_TOKENS,
        "derived": derived,
    }

    out = ROOT / "artifacts" / "calibration.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")

    print(_render(report))
    print(f"\nwrote {out.relative_to(ROOT)}")
    if args.write_doc:
        doc = write_doc(report)
        print(f"wrote {doc.relative_to(ROOT)}")

    if not args.check:
        return 0

    configured = {
        "read_timeout_ms": args.configured_read_ms,
        "total_budget_ms": args.configured_budget_ms,
        "stream_inter_chunk_timeout_s": args.configured_inter_chunk_s,
    }
    # Ceilings are computed against the CONFIGURED read timeout, not the derived
    # one: the question is what the system as deployed can serve.
    if args.configured_read_ms:
        derived["region_ceilings"] = [
            {
                "region": prof.region,
                "serviceable_max_tokens": min(
                    serviceable_tokens(prof, args.configured_read_ms), SCHEMA_MAX_TOKENS
                ),
                "schema_max_tokens": SCHEMA_MAX_TOKENS,
            }
            for prof in profiles
        ]
    problems = check(derived, {k: v for k, v in configured.items() if v is not None})
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        return 1
    print("PASS: every configured timeout is above its measured requirement.")
    return 0


def _render(report: dict) -> str:
    lines = [
        "",
        f"backend: {report['backend']}  models: {', '.join(report['models'])}",
        "",
        f"{'region':<16}{'ttft':>9}{'ms/tok':>9}"
        + "".join(f"{'p99@' + str(t):>11}" for t in report["token_steps"]),
    ]
    for r in report["regions"]:
        row = f"{r['region']:<16}{r['ttft_ms']:>8.1f}ms{r['ms_per_token']:>9.3f}"
        row += "".join(f"{s['p99_ms']:>9.1f}ms" for s in r["steps"])
        lines.append(row)

    lines += [
        "",
        f"largest request each region can serve at read_timeout={report['ceiling_at_ms']}ms:",
    ]
    for r in report["regions"]:
        cap = r["serviceable_max_tokens_at_default"]
        note = "" if cap >= report["schema_max_tokens"] else "   <-- BELOW the API's limit; 503s"
        lines.append(f"  {r['region']:<18}{cap:>5} tokens{note}")

    d = report["derived"]
    lines += [
        "",
        f"worst p99 across all regions and token counts: {d['measured_worst_p99_ms']}ms",
        (
            f"streaming inter-chunk gap p99:                 {d['measured_stream_gap_p99_ms']}ms"
            if d["measured_stream_gap_p99_ms"] is not None
            else "streaming inter-chunk gap p99:                 NOT MEASURED (0 chunks seen)"
        ),
        "",
        f"derived at {d['headroom']}x headroom:",
        f"  MRFP_READ_TIMEOUT_MS               = {d['read_timeout_ms']}",
        f"  MRFP_TOTAL_BUDGET_MS               = {d['total_budget_ms']}",
        (
            f"  MRFP_STREAM_INTER_CHUNK_TIMEOUT_S  = {d['stream_inter_chunk_timeout_s']}"
            if d["stream_inter_chunk_timeout_s"] is not None
            else "  MRFP_STREAM_INTER_CHUNK_TIMEOUT_S  = (not derived: nothing streamed)"
        ),
    ]
    return "\n".join(lines)


BLOCK = re.compile(
    r"(?P<open><!-- AUTOGEN:BEGIN:(?P<name>[\w-]+) -->)"
    r"(?P<body>.*?)"
    r"(?P<close><!-- AUTOGEN:END -->)",
    re.DOTALL,
)


def doc_blocks(report: dict) -> dict[str, str]:
    d = report["derived"]
    ceiling = report["ceiling_at_ms"]

    env = (
        f"| what was measured | value |\n|---|---|\n"
        f"| backend | `{report['backend']}` |\n"
        f"| model(s) | {', '.join('`' + m + '`' for m in report['models'])} |\n"
        f"| samples per step | {report['samples_per_step']} |\n"
        f"| token steps | {', '.join(str(t) for t in report['token_steps'])} |\n"
    )

    header = "| region | TTFT | ms/token |" + "".join(
        f" p99 @ {t} |" for t in report["token_steps"]
    )
    sep = "|---|---|---|" + "---|" * len(report["token_steps"])
    rows = []
    for r in report["regions"]:
        row = f"| {r['region']} | {r['ttft_ms']:.1f} ms | {r['ms_per_token']:.3f} |"
        row += "".join(f" {s['p99_ms']:.1f} ms |" for s in r["steps"])
        rows.append(row)
    table = "\n".join([header, sep, *rows])

    ceil_rows = []
    for r in report["regions"]:
        cap = r["serviceable_max_tokens_at_default"]
        verdict = (
            "serves the full range"
            if cap >= report["schema_max_tokens"]
            else f"**503s above {cap} tokens**"
        )
        ceil_rows.append(f"| {r['region']} | {cap} | {verdict} |")
    ceilings = "\n".join(
        [
            f"At `read_timeout_ms={ceiling}`, against an API that accepts up to "
            f"`max_tokens={report['schema_max_tokens']}`:",
            "",
            "| region | largest serviceable request | verdict |",
            "|---|---|---|",
            *ceil_rows,
        ]
    )

    derived = "\n".join(
        [
            f"Worst p99 across all regions and token counts: **{d['measured_worst_p99_ms']} ms**. "
            + (
                f"Streaming inter-chunk gap p99: **{d['measured_stream_gap_p99_ms']} ms** "
                f"over {d['stream_samples']} chunks."
                if d["measured_stream_gap_p99_ms"] is not None
                else "Streaming was **not measured** — no chunks were observed, so no "
                "inter-chunk timeout is derived here."
            ),
            "",
            f"At {d['headroom']}x headroom:",
            "",
            "```",
            f"MRFP_READ_TIMEOUT_MS              = {d['read_timeout_ms']}",
            f"MRFP_TOTAL_BUDGET_MS              = {d['total_budget_ms']}",
            f"MRFP_STREAM_INTER_CHUNK_TIMEOUT_S = {d['stream_inter_chunk_timeout_s']}"
            if d["stream_inter_chunk_timeout_s"] is not None
            else "MRFP_STREAM_INTER_CHUNK_TIMEOUT_S = (not derived: nothing streamed)",
            "```",
        ]
    )

    return {
        "calibration-env": env,
        "calibration-table": table,
        "calibration-ceilings": ceilings,
        "calibration-derived": derived,
    }


def write_doc(report: dict) -> Path:
    path = ROOT / "docs" / "TIMEOUTS.md"
    blocks = doc_blocks(report)
    text = path.read_text()

    def replace(match: re.Match) -> str:
        body = blocks.get(match.group("name"))
        if body is None:
            return match.group(0)
        # Rebuild from the NAMED groups. Indexing positionally here is how the
        # chaos report generator once dropped every closing marker: the nested
        # `name` group shifts the numbering.
        return f"{match.group('open')}\n{body}\n{match.group('close')}"

    path.write_text(BLOCK.sub(replace, text))
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=20, help="requests per token step")
    ap.add_argument("--attempts", type=int, default=2, help="regions the prod profile attempts")
    ap.add_argument("--connect-ms", type=int, default=100)
    ap.add_argument("--check", action="store_true", help="exit non-zero if config is unsupported")
    ap.add_argument(
        "--write-doc", action="store_true", help="rewrite the AUTOGEN blocks in docs/TIMEOUTS.md"
    )
    ap.add_argument(
        "--ceiling-at-ms",
        type=int,
        default=200,
        help="read timeout to report per-region token ceilings against (default: the brief's 200)",
    )
    ap.add_argument("--configured-read-ms", type=int, default=None)
    ap.add_argument("--configured-budget-ms", type=int, default=None)
    ap.add_argument("--configured-inter-chunk-s", type=float, default=None)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
