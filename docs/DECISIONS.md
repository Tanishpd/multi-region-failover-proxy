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
falling back to something plausible. The test-database safety rails (db 15, sentinel key, no
`FLUSHALL`) are frozen in [CONTRACT §10](CONTRACT.md#10-test-database-and-safety-rails). Pin
`redis:7.2-alpine` to match the host version, so `redis-cli` experiments share Lua and `TIME`
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
`SET`) has a lost-update race that any concurrent burst can hit.

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
resilience path.

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
the metric names, and arm64 manifest risk.

**Decision.** Keep `/metrics` — the chaos assertions read it. Cut the Prometheus and Grafana
containers. The chaos harness's own ASCII timeline is the visual artifact, and it has the advantage of
pasting directly into markdown and surviving in a terminal transcript.

**Consequence.** No graphs. A timeline table should communicate the failover moment at least as
well as a dashboard screenshot — and unlike a screenshot, it is reproducible and pastes into
markdown.

---

<a id="adr-011"></a>

## ADR-011: Regions get `restart: "no"` and `--timeout-graceful-shutdown 0`

**Date:** 2026-07-27 · **Status:** accepted

**Context.** Two Docker behaviours can quietly sabotage a chaos experiment.

A `restart: on-failure` or `always` policy resurrects a region that *crashes* mid-run, and `always`
additionally revives stopped containers when the daemon restarts — either invalidates the timeline
mid-measurement. (A policy does **not** fire after a manual `docker stop`: the daemon marks the
container manually stopped, so the chaos stop itself is safe under any policy. `restart: "no"`
removes the crash and daemon-restart paths and documents the intent.) And uvicorn's default
graceful shutdown waits for in-flight connections to close —
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

**Context.** The brief specifies a 200 ms timeout and `docker stop` as the fault injection — and
those two never meet: `docker stop` fails via DNS withdrawal in ~2 ms, before the read timeout can
fire ([LEARNING §3](LEARNING.md#3-failure-modes-are-not-interchangeable)). Shipping only the brief's
scenario would leave the headline timeout untested.

**Decision.** Add `pause-us-east` as a first-class scenario — `docker pause` produces a genuine
`ReadTimeout` at the 200 ms mark. Add a `blackhole` chaos mode on the region itself for the same
effect without needing the Docker CLI.

**Consequence.** Two scenarios instead of one: the same code path producing a ~2 ms failure and a
~200 ms failure depending on how the region died.

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

**Context.** The instinct is to persist. It's wrong here in three separate ways: every key already
has a TTL, so nothing has a lifetime worth preserving; both consumers fail open, so losing state
degrades gracefully; and the `proxy-restart` chaos drill wants a fast restart with a
guaranteed-empty script cache, because that is what exercises the `NOSCRIPT` recovery path — AOF
would slow the restart and defeat the drill.

**Decision.** `--save "" --appendonly no --maxmemory 256mb --maxmemory-policy volatile-ttl`, and no
named volume.

**Consequence.** All rate-limit state is lost on any Redis restart; both consumers tolerate that by
design.

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

---

<a id="adr-017"></a>

## ADR-017: The control plane is disabled by default, never open

**Date:** 2026-08-07 · **Status:** accepted

**Context.** A production-readiness audit found `/admin/regions`, `/admin/ratelimit`,
`/debug/whoami` and `/metrics` all answering 200 to anyone who could reach the proxy — and
`/admin/*` is exempt from rate limiting. `DELETE /admin/ratelimit?id=<self>` therefore resets your
own bucket for free: measured, an honest client got 20 of 40 requests through a 20/s limit while an
attacker resetting between batches got **100 of 100**. There was no auth primitive anywhere in
`proxy/app/` to build on.

**Decision.** `MRFP_ADMIN_TOKEN` guards `/admin/*` and `/debug/*`, compared with
`secrets.compare_digest`. **Unset means the routes return 404**, not that they stand open — a
control plane that can mutate rate-limit state must not be reachable because someone forgot to set a
variable. `/metrics` has its own switch (`MRFP_METRICS_PUBLIC`, default on) because scrapers often
cannot present a bearer token and it mutates nothing. `/debug/whoami` no longer echoes the trusted-
proxy list, which told an attacker the spoof would be honoured. `?id=` is validated as an IP.

**Consequence.** `make chaos` can no longer read the mode from `/admin/regions`; it reads
`MRFP_MODE` off the container with `docker inspect` instead, needing no credential.

---

<a id="adr-018"></a>

## ADR-018: Trust only the real ingress, and test the SHIPPED config

**Date:** 2026-08-07 · **Status:** accepted

**Context.** `docker-compose.yml` set `MRFP_TRUSTED_PROXIES` to
`127.0.0.1/32,::1/128,172.16.0.0/12` so that XFF would be honoured for host traffic. That range
contains the Docker bridge gateway every external request arrives from — so every client was a
trusted proxy and could name its own rate-limit identity. Measured: an honest client got 21 of 60
through a 20/s limit; a client rotating `X-Forwarded-For` got **60 of 60**.

`CONTRACT.md §7` says "never widen to RFC1918" and `config.py` defaults to loopback. The compose
file contradicted both.

**The part worth remembering.** `test_spoofing_cannot_mint_fresh_buckets` existed and passed
throughout. It passed because it built its own limiter with a LOOPBACK trust list. The property was
correct, the code implementing it was correct, and production was still wide open — because the test
chose a configuration the deployment does not use.

**Decision.** Loopback only, overridable with the real ingress address when there is one.
`tests/unit/test_hardening.py` now parses `docker-compose.yml` and asserts against the value that
actually ships, rather than a value the test picked.

**Consequence.** Without a real proxy in front, all host traffic shares the gateway's bucket — which
is exactly what CONTRACT §7 already documented. The demo's 429 burst must be fired in parallel,
which it already was.

---

<a id="adr-019"></a>

## ADR-019: Bound the request body before reading it, not after

**Date:** 2026-08-07 · **Status:** accepted

**Context.** `ProxyRequest.from_starlette` was `body = await request.body()` followed by a length
check. The check never protected anything: Starlette accumulates every chunk and joins them with no
limit, so the cap only ever ran against a body already resident in memory — and peak is roughly
twice the body, since the chunk list and the joined bytes coexist during the join. A single 512 MiB
POST drove the container to its cgroup limit and it was OOM-killed; `restart: "no"` meant it stayed
dead. ADR-006's claimed "256 KiB × 128 in-flight = bounded memory" ceiling did not exist.

The admission semaphore made it worse: the slot is acquired *before* the read, and there was no
inbound read deadline, so 130 sockets trickling ~130 bytes/second took `/v1/infer` to 100% 503 while
`/healthz` still reported 200.

**Decision.** Three bounds, cheapest first: reject on `Content-Length` before a byte is read; cap
incrementally across `request.stream()` for chunked bodies that declare no length or lie about it;
and wrap the whole read in `asyncio.timeout(MRFP_BODY_READ_TIMEOUT_S)`.

**Consequence.** Measured after the fix: a 400 MB body moves container memory by 0.22 MiB and
returns 413; 130 slow sockets leave the data plane serving 10 of 10 probes.

---

<a id="adr-020"></a>

## ADR-020: Refuse to boot on a misconfiguration

**Date:** 2026-08-07 · **Status:** accepted

**Context.** pydantic-settings silently ignores an unrecognised variable, so `MRFP_MODE=resilent`
meant literal, `MRFP_RL_FAIL_MODE=close` meant fail-open, and an unset `MRFP_REDIS_URL` meant the
token bucket quietly became per-process — with N replicas enforcing N times the configured limit,
signalled only by `redis: false` in one log line. `config.py` even carried a comment claiming an
unset value would raise at boot. It did not.

**Decision.** `Settings.validate_for_boot()` returns the problems that must stop the process:
unknown enum values, missing Redis (waivable with `MRFP_REQUIRE_REDIS=0`), a total budget too small
for `3 × (connect + read)` — which would silently mean the third region is never attempted — chaos
mode armed without an explicit token, and a non-positive rate limit. The lifespan logs each and
raises.

**Consequence.** A typo is now a failed deploy instead of a silent behaviour change.

---

<a id="adr-021"></a>

## ADR-021: A profile split, not a compromise

**Date:** 2026-08-07 · **Status:** accepted

**Context.** Production and the brief want genuinely different systems. A 200 ms
read timeout is right for a mock region behind a `sleep` and absurd for real inference, where first
token latency is seconds. `container_name` is pinned so `docker stop us-east-1-endpoint` works
verbatim — and pinning it forbids replicas and rolling replacement. `MRFP_MODE=literal` is
deliberately worse engineering that exists so the graded `except` block visibly fires. Degrading
either side to satisfy the other loses something real.

**Decision.** Two axes. `MRFP_PROFILE` ∈ `demo` | `prod` selects a settings preset, and
`deploy/compose.prod.yml` is a **standalone** topology.

Two mechanical details make it safe rather than merely tidy:

- The preset source is registered **below** `env_settings`, so an explicit environment variable
  always wins. `docker-compose.yml` pins `MRFP_READ_TIMEOUT_MS=200`, so even
  `MRFP_PROFILE=prod` against the demo stack cannot move a graded number.
- The prod topology is a separate FILE, not an override. Compose merge can add and replace keys but
  cannot **delete** them, and the two keys production must not have — `container_name` and the
  published `ports` — are exactly the ones the demo requires. An override file is structurally
  incapable of expressing the production shape.

The `demo` preset is **empty**: the demo profile *is* the shipped field defaults. A second copy of
the graded numbers would be a second thing that can drift away from them.

The profile changes CONFIGURATION, never CODE PATH. `if settings.profile ==` outside
`validate_for_boot()` is forbidden — the moment the two profiles take different branches, only one
of them is being tested.

**Consequence.** Two compose files that can drift, paid for by `test_brief_freeze.py`, which asserts
the shared keys stay in step and that prod has zero pinned container names. And the honest cost:
**the production configuration is the one `make chaos` never exercises at graded thresholds** — a
30 s read timeout makes a "transition p50 ≈ 200 ms" assertion meaningless. CI runs the matrix under
prod with categorical assertions only.

---

<a id="adr-022"></a>

## ADR-022: One pure-ASGI middleware instead of three BaseHTTPMiddleware layers

**Date:** 2026-08-07 · **Status:** accepted

**Context.** Each `BaseHTTPMiddleware` wraps the call in an anyio task group and pumps the response
through a memory object stream. Measured on this machine with `httpx.ASGITransport`:

```
bare Starlette                    126 us/req
3 x BaseHTTPMiddleware            599 us/req   overhead 473
1 x pure-ASGI ContextMiddleware   151 us/req   overhead  25
```

473 µs of pure CPU per request caps one worker near 1,700 rps before any proxying happens.

Separately, `proxy_requests_total` was incremented only inside `ResilientRouter.route`, so every
429, 413, 408, 404 and shed 503 was invisible to any availability calculation — the SLI could not
be computed at all.

**Decision.** One pure-ASGI layer doing request-id, client identity, rate limiting and response
telemetry. It is the outermost thing that sees a terminal status, so it is also the only place that
can count one. Fixing throughput and making the SLIs honest turned out to be the same edit.

**Consequence.** ~18× less middleware overhead, and a `proxy_http_responses_total` labelled by
client-visible outcome. The middleware-ordering trap disappears with the layers: ordering is now
explicit statements inside one `__call__` rather than an implicit consequence of `add_middleware`
prepending. `request_id.py` and `ratelimit_mw.py` are deleted; `client_identity.py` keeps its pure
functions, which is the part that was ever worth testing in isolation.

---

<a id="adr-023"></a>

## ADR-023: Saturation is not a region failure

**Date:** 2026-08-07 · **Status:** accepted

**Context.** `max_inflight` (128) exceeded the per-region connection pool (64). Above the pool,
overload surfaces as `PoolTimeout` — which the router recorded as a region failure. That opens a
breaker on a region **that was never contacted**, and purges its warm connection pool at exactly
peak load. The remaining regions absorb the traffic and follow. Load becomes a self-inflicted
multi-region outage, and every signal says the regions failed.

**Decision.** `pool_timeout` and `attempt_deadline` are `SELF_INFLICTED`: they never write region
health and never vote to the breaker. They are the only two codes in the contract's vocabulary that
a region outage physically cannot produce, which also makes them a nearly false-positive-free page
(`MRFPSelfInflictedSaturation`). Admission is additionally derived from the smallest pool when
`MRFP_MAX_INFLIGHT=0`, so overload sheds cleanly at the gate instead of manufacturing timeouts.

**Consequence.** Overload now produces a fast 503 at admission with `proxy_admission_rejected_total`
rising, and `proxy_region_pool_waiters` goes positive before any of it — the leading indicator.

---

<a id="adr-024"></a>

## ADR-024: Streaming, and the commit latch

**Date:** 2026-08-07 · **Status:** accepted

**Context.** Streaming is the dominant real use case for a model API, and it appears to contradict
[ADR-006](#adr-006): request bodies are buffered *precisely so* a failed attempt can be replayed to
another region, and a streamed response cannot be replayed once the client has seen part of it.

The resolution is to notice that **replayability is not a property of the request. It is a property
of how far the response has already travelled.**

**Decision.** The proxy pulls the upstream's headers **and its first chunk** before writing a single
byte downstream. That makes the entire time-to-first-byte window replayable:

| Phase | Meaning | Failover |
|---|---|---|
| `preroll` | headers + first chunk received, nothing sent downstream | **possible, and invisible** |
| `committed` | at least one byte written to the client | **structurally impossible** |

Pulling the first chunk rather than only the headers is what makes this worth having: an upstream
that returns `200 text/event-stream` instantly and then dies before producing a token is the most
likely mid-deploy failure shape, and it lands entirely inside the replayable window.

After commit, a failure emits an in-band `event: error` frame and then **raises**. Raising is the
point: returning would let uvicorn write the terminal zero-length chunk and tell the client it
received a complete answer. Truncation is therefore signalled twice — no `[DONE]` sentinel for an
SSE client, and an incomplete body for an HTTP/1.1-correct one — because either signal alone is
missable by a reasonable client.

Two timers, not one. Time-to-first-byte bounds the replayable window; a separate inter-chunk timer
bounds silence afterwards. The inter-chunk timer wraps **only the upstream read**, never the
downstream yield — wrapping the yield would make a slow *client* look like a dead region and
truncate a perfectly healthy generation.

**Consequence.** Streaming is off in the demo profile, so the graded behaviour is unchanged by its
presence rather than merely equivalent to it. Verified live: a region stopped before the first byte
produced `us-east-1:connect_error(31.9ms),eu-west-1:ok(97.1ms)` and a normal `[DONE]` stream, while
the same region dying mid-body produced three tokens, an `event: error` frame, zero `[DONE]`, and
**no failover**. Chaos scenario `truncate-mid-stream` asserts both (A17, A18); ids start at 17
because A14–A16 belong to the blackout set.

Mid-stream resume — reconnecting to a second region and continuing from a token offset — was
considered and rejected. It only works because this mock's inference is `blake2b`-deterministic. A
real model behind a different GPU, batch composition or sampler is not, so building it here would
encode an assumption that dissolves the moment the mock is replaced. Commit is commit.
