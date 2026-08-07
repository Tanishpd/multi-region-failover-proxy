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
below says `backend: mock`, and numbers derived from the mock describe the mock.
This is a procedure that *can* measure production, not evidence *about*
production. Point it at a real endpoint and re-read this file:

```
MRFP_UPSTREAM_BASE_URL=https://api.example.com \
MRFP_UPSTREAM_MODEL=your-model \
MRFP_UPSTREAM_API_KEY=sk-... \
  docker compose up -d && make calibrate
```

It also measures **service** latency at low concurrency, which is what a
per-attempt timeout must accommodate. Latency under load is a different
question; `chaos/capacity.py` and [docs/SLO.md](SLO.md) answer that one.

## The finding that mattered

A read timeout is usually reasoned about as *how long before I give up on a sick
region*. It is also, silently, a **capability ceiling**: any request whose honest
service time exceeds it can never be served by that region, however healthy the
region is.

At the brief's 200 ms, measured on this stack:

- ap-southeast-1 answers 8 tokens in ~183 ms and 512 tokens in ~310 ms;
- so a 512-token request that fails over to it returns **503 — from a region
  that was working perfectly**.

Reproduced directly:

```
$ docker stop us-east-1-endpoint eu-west-1-endpoint

$ curl -si localhost:8000/v1/infer -d '{"prompt":"hi","max_tokens":8}'
HTTP/1.1 200 OK
x-failover-path: us-east-1:connect_error(15.7ms),eu-west-1:connect_error(4.0ms),ap-southeast-1:ok(183.4ms)

$ curl -si localhost:8000/v1/infer -d '{"prompt":"hi","max_tokens":512}'
HTTP/1.1 503 Service Unavailable
x-failover-path: us-east-1:connect_error(7.7ms),eu-west-1:connect_error(6.3ms),ap-southeast-1:read_timeout(201.8ms)
```

This was invisible before latency scaled with tokens, because every request cost
the same. It is **not** a bug in the demo profile — 200 ms is the brief's number
and is frozen ([CONTRACT §2](CONTRACT.md)) — but it is a real property of that
number, and the production profile's larger timeout does not have it. See
[ADR-028](DECISIONS.md#adr-028).

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
| us-east-1 | 24.7 ms | 0.253 | 31.5 ms | 48.3 ms | 62.2 ms | 160.0 ms |
| eu-west-1 | 72.4 ms | 0.246 | 79.7 ms | 88.0 ms | 114.2 ms | 207.8 ms |
| ap-southeast-1 | 169.3 ms | 0.251 | 182.9 ms | 191.5 ms | 212.3 ms | 303.2 ms |
<!-- AUTOGEN:END -->

### Capability ceiling at the shipped read timeout

<!-- AUTOGEN:BEGIN:calibration-ceilings -->
At `read_timeout_ms=200`, against an API that accepts up to `max_tokens=512`:

| region | largest serviceable request | verdict |
|---|---|---|
| us-east-1 | 512 | serves the full range |
| eu-west-1 | 512 | serves the full range |
| ap-southeast-1 | 122 | **503s above 122 tokens** |
<!-- AUTOGEN:END -->

### Derived settings

<!-- AUTOGEN:BEGIN:calibration-derived -->
Worst p99 across all regions and token counts: **303.22 ms**. Streaming inter-chunk gap p99: **7.99 ms**.

At 2.0x headroom:

```
MRFP_READ_TIMEOUT_MS              = 607
MRFP_TOTAL_BUDGET_MS              = 1414
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
