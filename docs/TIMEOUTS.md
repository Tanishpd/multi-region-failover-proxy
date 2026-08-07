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
`llama3.2:3b` served by a local Ollama. Reproduce with any OpenAI-compatible
endpoint:

```
ollama pull llama3.2:3b         # or point at any OpenAI-compatible API
MRFP_UPSTREAM_BASE_URL=http://host.docker.internal:11434 \
MRFP_UPSTREAM_MODEL=llama3.2:3b \
  docker compose up -d && bash ops/scripts/calibrate.sh --samples 4 --yes
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
2. **Run-to-run variance is large.** Two consecutive runs against the same
   smaller model (`qwen2.5:0.5b`) derived 15313 ms and 7929 ms — a 1.9x spread,
   from nothing but contention on a shared machine. This is *why* the headroom
   multiple exists, and why a single run is a starting point rather than an
   answer. The run below used `--samples 4`, at which the reported "p99" is
   simply the observed maximum.
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

### The guess, checked

The production profile shipped `MRFP_READ_TIMEOUT_MS=30000` and
`MRFP_TOTAL_BUDGET_MS=64000`, and I said plainly that nobody had measured
anything to arrive at them.

Measured against `llama3.2:3b` — TTFT ~600-690 ms, ~18-23 ms per token, p99 of a
512-token request 12.7-14.9 s — the derived requirement is:

| setting | shipped guess | derived from measurement |
|---|---|---|
| `MRFP_READ_TIMEOUT_MS` | 30000 | **29845** |
| `MRFP_TOTAL_BUDGET_MS` | 64000 | **59890** |

The guess was **right**, within 1% — **at concurrency 1**. That qualification is
the whole story, and it is not a small one.

### Concurrency changes the answer by more than model size does

`--concurrency` defaults to 1, which measures *service* latency: the floor a
per-attempt timeout must clear. Against a backend that queues, that default is a
**trap** — calibrate a shared endpoint while it is idle and you derive a timeout
that is too small at exactly the moment it matters.

Same model, same host, same sweep — a lighter one (`--samples 3`) run twice back
to back, so the absolute figures differ slightly from the table above. Only
concurrency changed between the two rows:

| concurrency | ms/token | p99 @ 512 tokens | derived `read_timeout_ms` |
|---|---|---|---|
| 1 | 14.9 | 8.4 s | **24189** |
| 4 | 42.2 | 35.7 s | **80042** |

**3.3x**, from contention alone. Model size moved the requirement 4x (0.5B wanted
~8 s, 3B wants ~30 s); concurrency moved it almost as much. So the shipped 30000
is right for a backend serving roughly one request at a time and is **2.6x too
small** for the same backend at four.

The rule that follows: **calibrate at the peak concurrency you expect per
region**, not at idle. A latency figure without the concurrency it was measured
at is not reproducible, so the report records it and so does this table.

What changed overall is not that the numbers moved — they did not — but that
they stopped being assertions. `make calibrate-check` fails if either drifts
below what the backend needs, and re-deriving against your own hardware and your
own load is one command.

## Measured

<!-- AUTOGEN:BEGIN:calibration-env -->
| what was measured | value |
|---|---|
| backend | `upstream` |
| model(s) | `llama3.2:3b` |
| samples per step | 4 |
| concurrency | 1 |
| token steps | 8, 32, 128, 512 |

<!-- AUTOGEN:END -->

### Latency against requested tokens

<!-- AUTOGEN:BEGIN:calibration-table -->
| region | TTFT | ms/token | p99 @ 8 | p99 @ 32 | p99 @ 128 | p99 @ 512 |
|---|---|---|---|---|---|---|
| us-east-1 | 491.9 ms | 22.004 | 502.2 ms | 1154.0 ms | 3883.9 ms | 14922.4 ms |
| eu-west-1 | 621.1 ms | 19.923 | 395.5 ms | 1149.8 ms | 3934.3 ms | 14259.1 ms |
| ap-southeast-1 | 1995.2 ms | 22.644 | 482.8 ms | 2221.8 ms | 8700.3 ms | 13638.7 ms |
<!-- AUTOGEN:END -->

### Capability ceiling at the shipped read timeout

<!-- AUTOGEN:BEGIN:calibration-ceilings -->
At `read_timeout_ms=200`, against an API that accepts up to `max_tokens=512`:

| region | largest serviceable request | verdict |
|---|---|---|
| us-east-1 | 0 | **503s above 0 tokens** |
| eu-west-1 | 0 | **503s above 0 tokens** |
| ap-southeast-1 | 0 | **503s above 0 tokens** |
<!-- AUTOGEN:END -->

### Derived settings

<!-- AUTOGEN:BEGIN:calibration-derived -->
Worst p99 across all regions and token counts: **14922.39 ms**. Streaming inter-chunk gap p99: **50.57 ms** over 384 chunks.

At 2.0x headroom:

```
MRFP_READ_TIMEOUT_MS              = 29845
MRFP_TOTAL_BUDGET_MS              = 59890
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
