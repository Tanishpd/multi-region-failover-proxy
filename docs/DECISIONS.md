# DECISIONS

Architecture decision records. Written at decision time, not reconstructed afterwards — the point is
to preserve what was actually traded away, including the options that were nearly chosen.

Format: context → decision → consequence. Dates are when the call was made.

---

<a id="adr-001"></a>

## ADR-001: Contract-first

**Date:** 2026-07-27 · **Status:** accepted

**Context.** The design for this system was produced by four independent work streams (routing,
rate limiting, chaos/observability, repo/DX). Reviewing them together surfaced four incompatible
source trees, three different endpoint paths (`/v1/infer`, `/v1/predict`, `/v1/chat/completions`),
two competing header names for the single most important signal, three environment-variable
prefixes, two Redis container names, and two network names. Every one of those defects traces to the
same root cause: no artifact owned the cross-cutting names.

**Decision.** [CONTRACT.md](CONTRACT.md) is written and frozen *before* any application code. It owns
the tree, env prefix, endpoint schema, header names, container and network names, port map, Redis key
schema, and log event vocabulary. When code and contract disagree, the code is the bug.

**Consequence.** A documentation file is now a build dependency, which feels backwards until you
consider the alternative. Changing a header name means editing the contract first, which is the
intended friction.

---

<a id="adr-002"></a>

## ADR-002: Explicit Compose project name

**Date:** 2026-07-27 · **Status:** accepted

**Context.** The project lives in a directory called `Multi-Region Model Deployment & Failover Proxy`
— spaces and an ampersand. Compose derives its project name from the directory name, which here
produces something mangled and shell-hostile, and it leaks into generated container names, network
names, and volume names.

**Decision.** Set `name: mrfp` as the top-level key in `docker-compose.yml`, with
`COMPOSE_PROJECT_NAME=mrfp` in `.env` as a belt-and-braces fallback. Neither image's build context is
the repo root, so the awkward path never reaches a `COPY`. Shell scripts quote every expansion;
Python resolves paths from `__file__` or an env var, never the CWD; the chaos harness uses
`create_subprocess_exec(*argv)` and never `shell=True`.

**Consequence.** The directory can be renamed without breaking anything.

---

<a id="adr-003"></a>

## ADR-003: Fixed `container_name` on every service

**Date:** 2026-07-27 · **Status:** accepted

**Context.** Compose normally generates container names like `mrfp-us-east-1-endpoint-1`. The project
brief specifies `docker stop us-east-1-endpoint` literally, and the chaos harness runs exactly that
command.

**Decision.** Pin `container_name:` on all five services. The Redis container is named bare `redis`
rather than `mrfp-redis`, so `docker stop redis` also works verbatim — a reviewer will try it by
analogy with the brief.

**Consequence.** Only one instance of the stack can run at a time, and `docker compose up --scale` is
unavailable. Both are irrelevant here and the naming is load-bearing for the brief, so it's a clear
win.

---

<a id="adr-004"></a>

## ADR-004: Redis on host port 6380

**Date:** 2026-07-27 · **Status:** accepted

**Context.** This machine already runs a native homebrew `redis-server` (v7.2.7) listening on
127.0.0.1:6379 with real data in it. Publishing a container on 6379 would fail to bind — or worse,
the application could silently connect to the developer's own Redis and a test could flush it.

**Decision.** Publish `127.0.0.1:6380:6379`. In-network addressing stays `redis://redis:6379/0`.
`MRFP_REDIS_URL` deliberately has **no default**, so an unset variable raises at boot instead of
falling back to something plausible. Tests use db 15 and refuse to run unless the db index is 15 *and*
a sentinel key written by our own container is present. `FLUSHALL` appears nowhere in the repository.
Pin `redis:7.2-alpine` to match the host version, so `redis-cli` experiments share Lua and `TIME`
semantics with the container.

**Consequence.** Every host-side command needs `-p 6380`, which is a small ongoing tax in exchange for
not being able to destroy the user's data.

---

<a id="adr-005"></a>

## ADR-005: Lua token bucket, not INCR+EXPIRE

**Date:** 2026-07-27 · **Status:** accepted

**Context.** Three candidate implementations:

1. `INCR` + `EXPIRE` fixed window — two commands, trivially simple, but allows a 2× burst across a
   window boundary and gives no sensible `Retry-After`.
2. A sorted-set sliding log — exact, but stores one member per request and is O(n) memory per client.
3. A token bucket in a Lua script — one round trip, O(1) memory, natural burst semantics, exact
   `Retry-After`.

The brief asks specifically for a token bucket, and the naive Python implementation (`GET`, compute,
`SET`) has a lost-update race that the chaos flood hits on essentially every burst.

**Decision.** A Lua script executed via `EVALSHA`, performing refill, decision, debit, and TTL refresh
atomically in one round trip. Written for N layers but configured with exactly one.

**Consequence.** Logic in a second language, which is harder to test and to debug. Mitigated by
running the integration tests against a real Redis rather than a simulator — the Lua semantics are
precisely what's under test, so simulating them would be circular.

**Rejected along the way:** shipping two configured layers (burst + sustained). It's a better rate
limiter, but it means a reviewer has to reason about *which* bucket denied a request, and the brief is
"deduct a token; 429 at zero". The N-layer capability stays in the script and costs nothing.

---

<a id="adr-006"></a>

## ADR-006: Buffer request bodies

**Date:** 2026-07-27 · **Status:** accepted

**Context.** Failover means sending the same request to a second region. A streamed body is consumed
by the first attempt and cannot be replayed.

**Decision.** Read the body once into memory, capped at 256 KiB (above which: 413). Pair it with an
admission semaphore, since buffered bodies × unbounded concurrency is an unbounded memory commitment
in a 512 MB container.

**Consequence.** Large uploads and streaming responses are out of scope. For a mock inference API
that's free; for a real one it would need an idempotency-key design instead.

---

<a id="adr-007"></a>

## ADR-007: 4xx is not a failure; `CancelledError` is not caught

**Date:** 2026-07-27 · **Status:** accepted

**Context.** The obvious failover trigger is "anything that isn't a 200". That's wrong twice over.

**Decision.** A 4xx response means the region is alive and the caller sent something bad — pass it
through, record a *success* for the region, and do not retry. Only 5xx and upstream 429s count as
region failures. Separately, `asyncio.CancelledError` is a `BaseException` and is deliberately not
caught: it means the client hung up, and recording it would blame a healthy region.

**Consequence.** Under a flood where clients abandon requests, the breaker stays accurate instead of
opening on phantom failures.

---

<a id="adr-008"></a>

## ADR-008: In-process circuit breaker, not Redis-backed

**Date:** 2026-07-27 · **Status:** accepted

**Context.** A Redis-backed breaker with a Lua CAS was designed and specified in detail. Its entire
justification was correctness across `uvicorn --workers N`: with N workers and an in-process breaker,
detecting an outage costs N × threshold failures, and N workers each send their own half-open probe.

But the proxy is pinned to **one** worker anyway, because `prometheus-client` without
`PROMETHEUS_MULTIPROC_DIR` reports only one worker's counters — and under-reported metrics would
quietly corrupt every number in the chaos report.

**Decision.** One ~50-line in-process breaker class, used for both regions and Redis-as-a-dependency.
Document what changes at N workers rather than building for it.

**Consequence.** ~200 lines and eight tests not written, for a problem this deployment does not have.
It also removes an awkward property: a Redis-backed breaker makes Redis a dependency of the
resilience path, in a project whose whole point is not having a single point of failure.

---

<a id="adr-009"></a>

## ADR-009: `MRFP_MODE=literal` is the default; no active health poller

**Date:** 2026-07-27 · **Status:** accepted

**Context.** This is the sharpest tension in the project. A circuit breaker and an active health
poller are both correct engineering — and both would erase the behaviour the brief asks to see.

The brief requires that the `try`/`except` catches the connection failure and routes onward. With a
breaker, `us-east-1` opens after ~5 failures and subsequent requests are *skipped* rather than
attempted, so under a 200 RPS flood the `except` block fires for ~5 of ~3000 requests. With an active
health poller it's worse: the region is marked down before any data-plane request fails, so the
`except` block may never fire at all.

**Decision.** Three counter-measures:

1. The active health poller is **cut entirely**. Detection is purely passive.
2. `MRFP_MODE=literal` is the **default** and disables the breaker, so every request during an outage
   walks the priority list and genuinely enters the `except` block.
3. Chaos assertion **A13** requires `upstream_failover` log lines ≥ 95 % of the requests that failed
   over. If the except block is ever bypassed, `make chaos` fails.

The breaker ships as opt-in `resilient` mode. `make chaos` runs literal first, then resilient, and
reports the latency difference as a documented improvement *on top of* the satisfied requirement.
`region_skipped_breaker_open` is a distinct log event from `upstream_failover` so "caught it" and
"skipped it" can never be conflated.

**Consequence.** The default configuration is deliberately not the best-engineered one. That's
recorded here and in the README so it reads as a choice rather than an oversight.

---

<a id="adr-010"></a>

## ADR-010: No Prometheus or Grafana containers

**Date:** 2026-07-27 · **Status:** accepted

**Context.** A pre-provisioned Grafana dashboard is a strong demo artifact. It also means ~600 MB of
additional images on a machine with 5.5 GB free, provisioning YAML and dashboard JSON that drift from
the metric names, and arm64 manifest risk. The specified scrape config also had a latent defect —
`scrape_interval: 1s` against the default `scrape_timeout: 10s` is rejected outright by Prometheus, so
the profile would have been dead on first use.

**Decision.** Keep `/metrics` — the chaos assertions read it. Cut the Prometheus and Grafana
containers. The chaos harness's own ASCII timeline is the visual artifact, and it has the advantage of
pasting directly into markdown and surviving in a terminal transcript.

**Consequence.** No graphs. The timeline table turns out to communicate the failover moment better
than a dashboard screenshot anyway, because it's reproducible.

---

<a id="adr-011"></a>

## ADR-011: Regions get `restart: "no"` and `--timeout-graceful-shutdown 0`

**Date:** 2026-07-27 · **Status:** accepted

**Context.** Two Docker behaviours quietly sabotage a chaos experiment.

A `restart:` policy resurrects a container the harness just stopped, invalidating the timeline
mid-measurement. And uvicorn's default graceful shutdown waits for in-flight connections to close —
including the proxy's *warm keep-alive sockets*. The result is that `docker stop` blocks for the full
grace period while the region continues serving 200s. The demo shows no failover and the router looks
broken.

**Decision.** `restart: "no"` on the regions. `--timeout-graceful-shutdown 0` in the region's uvicorn
command. `stop_grace_period: 2s` — not `0s`, which would make `docker stop` behave like `docker kill`
and change which exception the client sees. Chaos assertion **A11** fails the run if `docker stop`
takes longer than 1.5 s, and names this fix in the failure message.

**Consequence.** Regions don't come back on their own; the harness restarts them explicitly, which is
what we want for a measured recovery phase.

---

<a id="adr-012"></a>

## ADR-012: `docker pause` as a second chaos primitive

**Date:** 2026-07-27 · **Status:** accepted

**Context.** The brief specifies a 200 ms timeout and specifies `docker stop` as the fault injection.
Those two things do not meet. `docker stop` causes Docker's embedded DNS resolver to withdraw the
container's record, so the client fails with `ConnectError` in about 2 ms — the read timeout is never
reached. Shipping only the brief's scenario would mean the headline timeout is completely untested.

**Decision.** Add `pause-us-east` as a first-class scenario. `docker pause` freezes the process
without closing sockets; the kernel still completes the handshake from the listen backlog, so connect
succeeds and the read hangs until the 200 ms timeout fires. Add a `blackhole` chaos mode on the region
itself for the same effect without needing the Docker CLI.

**Consequence.** Two scenarios instead of one, and the most interesting single output in the project:
the same code path producing a ~2 ms failure and a ~200 ms failure depending on how the region died.

---

<a id="adr-013"></a>

## ADR-013: Fail open when Redis is unavailable

**Date:** 2026-07-27 · **Status:** accepted

**Context.** If the rate limiter can't reach Redis, it either serves traffic unlimited (fail open) or
rejects everything (fail closed). Fail closed is the right default for a limiter protecting a scarce
resource; fail open is right for one protecting against abuse.

**Decision.** Fail **open** by default, configurable via `MRFP_RL_FAIL_MODE`. Adding a hard dependency
on Redis would introduce a new single point of failure into a system whose entire purpose is not
having one.

Critically, naive fail-open is still a *latency* outage — every request pays the socket timeout before
giving up. So Redis sits behind the same breaker class as the regions, with an in-process fallback
bucket, and `X-RateLimit-Mode` reports which of `redis` / `local` / `open` actually decided.

**Consequence.** A Redis outage degrades limiting accuracy rather than availability, and it's visible
in a response header rather than silent.

---

<a id="adr-014"></a>

## ADR-014: Redis is cache-only — no AOF, no volume

**Date:** 2026-07-27 · **Status:** accepted

**Context.** The instinct is to persist. It's wrong here in three separate ways.

**Decision.** `--save "" --appendonly no --maxmemory 256mb --maxmemory-policy volatile-ttl`, and no
named volume.

**Consequence.** Every key already has a TTL, so nothing has a lifetime worth preserving. Both
consumers fail open, so losing state degrades gracefully by design. And the `proxy-restart` chaos
drill specifically wants a fast restart with a guaranteed-empty script cache, because that's what
exercises the `NOSCRIPT` recovery path. AOF would slow the restart and defeat the drill.

---

<a id="adr-015"></a>

## ADR-015: Host venv on Python 3.13; containers on 3.12

**Date:** 2026-07-27 · **Status:** accepted

**Context.** The host runs Python 3.14.6 as its default (PEP 668 externally managed), with 3.13 and
3.12 also installed. No `uv`, no `pipx`. Anything that imports the application runs in a container
regardless — but the chaos harness needs the Docker CLI, so it must run on the host, and the unit
tests should run without Docker at all.

**Decision.** Containers pin `python:3.12-slim-bookworm`, native arm64. The host venv is created with
`/opt/homebrew/bin/python3.13` and installs only pure-Python packages (httpx, pytest, pytest-asyncio,
rich, pyyaml). Never set `platform: linux/amd64` — emulation jitter would make a 200 ms threshold
flap.

**Consequence.** Two Python versions in play, which is fine because they never share code — the host
venv only runs `chaos/` and `tests/unit/`.

---

<a id="adr-016"></a>

## ADR-016: One command per Make recipe

**Date:** 2026-07-27 · **Status:** accepted

**Context.** The standard Makefile hardening idiom is `.SHELLFLAGS := -eu -o pipefail -c`. macOS ships
**GNU Make 3.81**, and `.SHELLFLAGS` was introduced in 3.82. Below that it is not an error — it is
**silently ignored**. Verified directly on this machine: a recipe containing `false | true` continued
executing and the target exited 0.

For a project whose central claim is "`make chaos` exits non-zero if failover regresses", a Make
version that swallows failures is a correctness bug in the headline feature.

**Decision.** Do not rely on `.SHELLFLAGS`. Every recipe is a single command delegating to a script in
`ops/scripts/`, each carrying its own `set -euo pipefail`. `make doctor` warns when `make` is older
than 3.82.

**Consequence.** Slightly more files, and logic lives in shell scripts rather than the Makefile. In
exchange, exit codes are trustworthy — which is the entire point of the chaos harness.
