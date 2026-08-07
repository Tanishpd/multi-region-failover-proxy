# Timeouts, derived

Every number in the AUTOGEN blocks below was produced by `make calibrate`
against a running stack. Nothing here is hand-written, and nothing here should
be edited by hand — re-run the command instead.

## Why this file exists

`MRFP_READ_TIMEOUT_MS=200` is the brief's number, and it is defensible because
the brief says so. The production profile's `30000` was not: nobody measured
anything to arrive at it, and I said so plainly when asked whether the system
was production-grade.

Worse, **nothing in this repository could have justified it.** The mock region
slept for a constant regardless of how many tokens were requested, so the
measured p99 of an 8-token request equalled that of a 512-token one. Measuring a
constant tells you nothing about what generation costs, so any timeout derived
from it would have been a guess wearing a measurement's clothes.

Two changes fix that:

1. **Region latency now scales with the work requested** — `TTFT + n x ITL`,
   which is the shape real serving has. TTFT differs per region because network
   distance does; the per-token cost does not, because it is a property of the
   accelerator rather than the distance to it.
2. **The regions can forward to a real model** (`MRFP_UPSTREAM_BASE_URL`), so
   the same command that produced the numbers below can be pointed at a real
   endpoint and produce numbers worth deploying.

`make calibrate` sweeps `max_tokens` against each region directly — not through
the proxy, because a read timeout bounds one *region attempt* — fits
`latency ≈ ttft + slope x tokens`, and derives the timeouts that follow.
`make calibrate-check` does the same and **exits non-zero** if a configured
timeout is below what was measured.

## What it honestly does not tell you

It measures whatever is behind the regions, and it labels what that was. The run
below says `backend: upstream`, so these numbers came from a real model —
`qwen2.5:0.5b` served by a local Ollama. Reproduce with any OpenAI-compatible
endpoint:

```
ollama pull qwen2.5:0.5b        # or point at any OpenAI-compatible API
MRFP_UPSTREAM_BASE_URL=http://host.docker.internal:11434 \
MRFP_UPSTREAM_MODEL=qwen2.5:0.5b \
  docker compose up -d && make calibrate
```

**Against a paid or shared endpoint it will refuse until you confirm.** A default
sweep is **252 requests and ~43,000 generated tokens** — trivial in money against
most models, but it lands on someone else's rate limit, and it used to happen
silently. When any region reports it is proxying to a live model, the tool
prints the plan and exits 2:

```
plan: 252 requests, ~42,840 generated tokens across 3 regions at --samples 20

REFUSING: the regions are proxying to a live model (qwen2.5:0.5b).
This would send 252 real requests and generate ~42,840 tokens.
Re-run with --yes to confirm, or lower --samples first.
```

Lower `--samples` first against anything slow or expensive; note that with *n*
samples the reported p99 is simply the observed maximum.

**Three caveats that bound how far these numbers travel.**

1. **A small model on a laptop is not your production fleet.** Re-run this
   against the hardware you deploy on; that is the entire point of it being a
   command rather than a constant.
2. **Run-to-run variance is large.** Two consecutive runs against the same model
   derived 15313 ms and 7929 ms — a 1.9x spread, from nothing but contention on
   a shared machine. This is *why* the headroom multiple exists, and why a single
   run is a starting point rather than an answer.
3. **The per-region rows are one backend measured three times.** All three
   regions proxy to the same Ollama instance, so the spread in `ms/token`
   (0.759 / 1.036 / 2.224) is measurement noise, not geography. Against genuinely
   separate endpoints these rows would mean what they say.

It also measures **service** latency at low concurrency, which is what a
per-attempt timeout must accommodate. Latency under load is a different
question; `chaos/capacity.py` and [docs/SLO.md](SLO.md) answer that one.

## The finding that mattered

A read timeout is usually reasoned about as *how long before I give up on a sick
region*. It is also, silently, a **capability ceiling**: any request whose honest
service time exceeds it can never be served by that region, however healthy the
region is.

Against the **mock**, this showed up as an edge case: ap-southeast-1 answered 8
tokens in ~183 ms but 512 tokens in ~310 ms, so only the largest requests fell
off the cliff.

Against a **real model** it is not an edge case. Every region needs ~500 ms
before it emits its first token, so at a 200 ms read timeout the serviceable
request size is **zero** — the table below says `0 tokens` for all three.
End to end, with the regions pointed at Ollama:

```
$ curl -si localhost:8000/v1/infer -d '{"prompt":"Name three colours.","max_tokens":32}'
HTTP/1.1 503 Service Unavailable
x-served-by-region: none
x-failover-path: us-east-1:read_timeout(219.7ms),eu-west-1:read_timeout(208.5ms),ap-southeast-1:read_timeout(205.8ms)
```

Every region healthy. Every region timed out. **The brief's 200 ms cannot serve
a single real request**, and no measurement of the mock could have said so,
because the mock was configured to be fast.

This is not a bug in the demo profile — 200 ms is the brief's number and is
frozen ([CONTRACT §2](CONTRACT.md)) — and the mock it runs against genuinely
answers inside it. It is a statement about what that number means the moment
anything real is behind it. See [ADR-028](DECISIONS.md#adr-028).

For reference, the production profile's `30000` ms comfortably covers the
measured requirement of 7929 ms — so the guess was conservative rather than
wrong. What changed is that something now justifies it, and `make
calibrate-check` fails if it ever stops being true.

## Measured

<!-- AUTOGEN:BEGIN:calibration-env -->
| what was measured | value |
|---|---|
| backend | `mock` |
| model(s) | `mock-echo-1` |
| samples per step | 20 |
| token steps | 8, 32, 128, 512 |

<!-- AUTOGEN:END -->

### Latency against requested tokens

<!-- AUTOGEN:BEGIN:calibration-table -->
| region | TTFT | ms/token | p99 @ 8 | p99 @ 32 | p99 @ 128 | p99 @ 512 |
|---|---|---|---|---|---|---|
| us-east-1 | 23.6 ms | 0.257 | 30.4 ms | 36.4 ms | 62.1 ms | 170.4 ms |
| eu-west-1 | 72.9 ms | 0.248 | 81.5 ms | 89.2 ms | 115.8 ms | 208.2 ms |
| ap-southeast-1 | 165.9 ms | 0.261 | 179.7 ms | 186.1 ms | 212.2 ms | 312.0 ms |
<!-- AUTOGEN:END -->

### Capability ceiling at the shipped read timeout

<!-- AUTOGEN:BEGIN:calibration-ceilings -->
At `read_timeout_ms=200`, against an API that accepts up to `max_tokens=512`:

| region | largest serviceable request | verdict |
|---|---|---|
| us-east-1 | 512 | serves the full range |
| eu-west-1 | 511 | **503s above 511 tokens** |
| ap-southeast-1 | 130 | **503s above 130 tokens** |
<!-- AUTOGEN:END -->

### Derived settings

<!-- AUTOGEN:BEGIN:calibration-derived -->
Worst p99 across all regions and token counts: **312.03 ms**. Streaming inter-chunk gap p99: **13.82 ms** over 384 chunks.

At 2.0x headroom:

```
MRFP_READ_TIMEOUT_MS              = 625
MRFP_TOTAL_BUDGET_MS              = 1450
MRFP_STREAM_INTER_CHUNK_TIMEOUT_S = 1.0
```
<!-- AUTOGEN:END -->

The headroom multiple exists because a timeout set at exactly p99 fails 1% of
healthy requests by construction. It absorbs GC pauses, a noisy neighbour, and
the drift between the day you measure and the day it matters.

## Related

- [ADR-028](DECISIONS.md#adr-028) — deriving timeouts instead of asserting them
- [docs/SLO.md](SLO.md) — capacity and latency under load
- [docs/ARCHITECTURE.md](ARCHITECTURE.md) — the timeout budget arithmetic
