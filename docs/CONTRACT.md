# CONTRACT

**Status: FROZEN.** This file is written before any application code and is the single source of
truth for every name that crosses a module, container, or process boundary.

Anything in here may only change by editing *this file first*, then propagating. If two files
disagree, this one wins and the other is a bug. Where a name is load-bearing for the project brief
(container names, the 200 ms timeout, the priority order), it is marked **[BRIEF]** and must not
change at all.

---

## 1. Repository tree

```
proxy/     the FastAPI failover proxy      (container: mrfp-proxy)
region/    ONE parameterized mock region   (containers: us-east-1-endpoint, ...)
chaos/     the chaos harness               (runs on the HOST, needs the docker CLI)
ops/       shell scripts + bench payloads  (no Python)
tests/     unit (no Docker) / integration (needs Redis) / e2e (needs the stack)
config/    regions.yaml — the region registry
docs/      this file and its siblings
artifacts/ chaos output; contents gitignored, directory tracked
```

Build contexts are `./proxy` and `./region`. Neither image ever copies from the repo root, so the
directory name — which contains spaces and an `&` — never reaches a `COPY` instruction.

---

## 2. Configuration

**Every** environment variable is prefixed `MRFP_`. Nested settings use a `__` delimiter
(`MRFP_BREAKER__FAILURE_THRESHOLD`). There is no second prefix; `PROXY_`, `RL_`, and bare names are
forbidden.

> Why this is strict: pydantic-settings **silently ignores** an unrecognised prefix. A typo'd
> variable does not error — it leaves the default in place and you debug the wrong thing for an
> hour. To make that impossible to miss, `proxy/app/main.py` logs the fully-resolved settings object
> as its very first log line on boot.

| Variable | Default | Notes |
|---|---|---|
| `MRFP_MODE` | `literal` | `literal` \| `resilient`. **[BRIEF]** `literal` is the default and disables the circuit breaker, so every request during an outage walks the priority list and hits the `try/except`. See §9. |
| `MRFP_REDIS_URL` | **none — required** | Deliberately has no default. Unset raises at boot rather than silently connecting to the host's own Redis on 6379. |
| `MRFP_REGIONS_FILE` | `/config/regions.yaml` | Absolute, resolved from env — never `Path(__file__).parents[N]`, which breaks in-container. |
| `MRFP_REGION_PRIORITY` | unset | Comma-separated override of the YAML order. The *only* way to reorder regions. |
| `MRFP_READ_TIMEOUT_MS` | `200` | **[BRIEF]** the brief's number. |
| `MRFP_CONNECT_TIMEOUT_MS` | `100` | |
| `MRFP_POOL_TIMEOUT_MS` | `50` | |
| `MRFP_TOTAL_BUDGET_MS` | `900` | Ceiling across all failover attempts. |
| `MRFP_MAX_INFLIGHT` | `128` | Admission semaphore. |
| `MRFP_MAX_BODY_BYTES` | `262144` | 256 KiB → 413 above it. Bodies are buffered so they can be replayed to a second region. |
| `MRFP_TRUSTED_PROXIES` | `127.0.0.1/32,::1/128` | See §7. Never widen to all of RFC1918. |
| `MRFP_RL_CAPACITY` | `20` | **[BRIEF]** token bucket size. |
| `MRFP_RL_REFILL_PER_SEC` | `20` | |
| `MRFP_RL_TTL_PAD_MS` | `2000` | Added to the computed full-refill TTL. |
| `MRFP_RL_FAIL_MODE` | `open` | `open` \| `closed`. Posture when Redis is unreachable. |
| `MRFP_BREAKER__FAILURE_THRESHOLD` | `5` | Resilient mode only: consecutive failures before the breaker opens. |
| `MRFP_BREAKER__COOLDOWN_S` | `5` | Resilient mode only: OPEN → HALF_OPEN delay; doubles after a failed probe. |
| `MRFP_LOG_LEVEL` | `INFO` | |
| `MRFP_CHAOS_MODE` | `0` | When `1`, `X-Chaos-Bypass` with the right token skips rate limiting. |
| `MRFP_CHAOS_TOKEN` | unset | Required if `MRFP_CHAOS_MODE=1`. |

Region-image variables (in `region/region.env` plus per-service overrides):

| Variable | Example | Notes |
|---|---|---|
| `MRFP_REGION_ID` | `us-east-1` | |
| `MRFP_REGION_ZONE` | `us-east-1a` | |
| `MRFP_BASE_LATENCY_MS` | `15` | Simulated per-region latency. Makes the priority order physically meaningful. |
| `MRFP_JITTER_MS` | `5` | |
| `MRFP_ADMIN_TOKEN` | `dev-chaos` | Guards `/admin/chaos`. |

---

## 3. HTTP contract

### `POST /v1/infer` — the only data-plane route

This path is identical on the proxy and on each region. The proxy forwards the body unchanged.

Request:

```json
{ "prompt": "hello", "max_tokens": 16, "seed": null }
```

- `prompt` — required, string, 1..8192 chars
- `max_tokens` — optional int, default `16`, 1..512
- `seed` — optional int or null

Response `200`:

```json
{
  "id": "inf_7f3c9a12",
  "region": "us-east-1",
  "zone": "us-east-1a",
  "model": "mock-echo-1",
  "completion": "…deterministic text…",
  "usage": { "prompt_tokens": 2, "completion_tokens": 16 },
  "latency_ms": 15.8
}
```

Inference is **deterministic**: `blake2b(prompt || seed)` seeds the RNG, so all three regions return
identical output for identical input. This is what makes replaying a POST to a second region
defensible, and it is why the chaos harness can diff responses across a failover without noise.

### Control-plane routes (proxy)

| Route | Purpose | Rate limited? |
|---|---|---|
| `GET /healthz` | Process liveness **only**. Returns 200 while every region is dead. | no |
| `GET /readyz` | 200 iff ≥1 region is currently believed usable; else 503. | no |
| `GET /metrics` | Prometheus text exposition. | no |
| `GET /admin/regions` | Per-region breaker state, last error, last success. | no |
| `GET /debug/whoami` | Echoes peer address, XFF chain, and the resolved bucket identity. | no |
| `GET /admin/ratelimit/bucket?id=` | Peek a bucket without consuming (`mode=peek`). | no |
| `DELETE /admin/ratelimit/bucket?id=` | Reset a bucket, for demos. | no |

### Control-plane routes (region)

| Route | Purpose |
|---|---|
| `GET /healthz` | 200 unless a chaos mode says otherwise. |
| `GET /` | Region metadata (`id`, `zone`, `base_latency_ms`, active chaos mode). |
| `POST /admin/chaos` | Set a mode. Token-guarded via `X-Admin-Token`. |
| `GET /admin/chaos` | Read the active mode. |
| `DELETE /admin/chaos` | Clear back to healthy. |

Chaos modes are exactly three: `slow` (add latency), `error` (return 503), `blackhole` (accept the
connection and never respond). No others.

`POST /admin/chaos` body: `{ "mode": "slow" | "error" | "blackhole", "ms": <int> }` — `ms` is the
added latency, required for `slow` and ignored otherwise. `GET /admin/chaos` returns
`{ "mode": <mode | null>, "ms": <int | null> }`.

---

## 4. Response headers

Set by the proxy on success **and on error responses (429 and 503)** — a failure response that
doesn't explain itself is useless during a demo.

| Header | Example | Meaning |
|---|---|---|
| `X-Served-By-Region` | `eu-west-1` | Which region produced this response. The chaos harness keys its entire timeline on this. |
| `X-Failover-Path` | `us-east-1:connect_error(2.1ms),eu-west-1:ok(63.4ms)` | Every attempt, in order, with outcome and duration. |
| `X-Attempts` | `2` | |
| `X-Total-Latency-Ms` | `65.9` | Proxy-observed wall clock. |
| `X-Degraded` | `0` \| `1` | 1 when served by a non-primary region. |
| `X-Breaker-State` | `closed` | State of the *serving* region's breaker. `n/a` in literal mode. |
| `X-Request-Id` | `8f2c1d…` | Echoed from the request or minted. |
| `X-RateLimit-Mode` | `redis` \| `local` \| `open` | Which limiter actually decided. |
| `RateLimit-Limit` | `20` | |
| `RateLimit-Remaining` | `13` | |
| `RateLimit-Reset` | `1` | Seconds until full. |
| `Retry-After` | `1` | On 429 and on 503 only. |

`X-RateLimit-Limit` / `-Remaining` / `-Reset` are also emitted as legacy aliases. There are **no**
`X-MRFP-*` headers.

### Error bodies

Both 429 and 503 return `application/problem+json` (RFC 9457):

```json
{
  "type": "https://mrfp.local/errors/rate-limit-exceeded",
  "title": "Rate limit exceeded",
  "status": 429,
  "detail": "Bucket empty for ip:127.0.0.1.",
  "retry_after_seconds": 1,
  "request_id": "8f2c1d…"
}
```

---

## 5. Container, network, and port map

**[BRIEF]** The three region container names are fixed by the project brief. The chaos script runs
`docker stop us-east-1-endpoint` literally, so these strings are part of the brief, not labels.

| Container | Image | Host port | In-network |
|---|---|---|---|
| `us-east-1-endpoint` | `mrfp/region:dev` | `9001` | `us-east-1-endpoint:8080` |
| `eu-west-1-endpoint` | `mrfp/region:dev` | `9002` | `eu-west-1-endpoint:8080` |
| `ap-southeast-1-endpoint` | `mrfp/region:dev` | `9003` | `ap-southeast-1-endpoint:8080` |
| `redis` | `redis:7.2-alpine` | `6380` | `redis:6379` |
| `mrfp-proxy` | `mrfp/proxy:dev` | `8000` | `mrfp-proxy:8000` |

- Compose project name: **`mrfp`** (set via the top-level `name:` key). Without it, Compose derives
  the project name from the directory — which here contains spaces and an `&`.
- Network: **`mrfp-net`**, a user-defined bridge so container-name DNS resolves.
- Redis is on host port **6380, never 6379** — a native homebrew `redis-server` already owns 6379 on
  this machine. Inside the network it is still `redis:6379`.
- The Redis container is named bare `redis` so `docker stop redis` works verbatim, in the same style
  as the brief's own `docker stop us-east-1-endpoint`.

**The proxy must reach regions by container name, never `localhost`.** Inside the proxy container,
`localhost` is the proxy itself. Host ports 9001–9003 exist only for humans poking with `curl`.

---

## 6. Redis key schema

```
rl:v1:{<dimension>:<identity>}:<layer>
```

- Braces are a Redis **hash tag** — everything inside is what hashes to a slot. Every key for one
  client therefore lands on one slot, which is what makes a multi-key Lua script legal if this ever
  moves to Cluster.
- `dimension` is `ip` (or `key` if API-key bucketing is enabled later).
- `layer` is `burst`. The Lua script is written for N layers; exactly one is configured.
- Value is a HASH: field `t` = tokens (string, `%.6f`), field `ts` = last refill (unix ms, `%.0f`).

Example: `rl:v1:{ip:127.0.0.1}:burst`

Databases: **0** = application. **15** = pytest, and *only* pytest — safety rails in §10.

---

## 7. Client identity

The rate limiter buckets by resolved client IP. Resolution is a **rightmost-untrusted walk**:

1. If the direct peer is not in `MRFP_TRUSTED_PROXIES`, use the peer. Never consult
   `X-Forwarded-For` from an untrusted peer — it is attacker-supplied.
2. Otherwise walk `XFF ++ [peer]` right-to-left and return the first entry that is *not* trusted.
3. If every entry is trusted, fall back to **the peer** — never `chain[0]`.

> Step 3 is the security-critical one. `chain[0]` is the leftmost, fully attacker-controlled value.
> Returning it means an attacker sends a different `X-Forwarded-For` on every request, mints a fresh
> full bucket each time, and the rate limiter does nothing at all.

Default trusted set is loopback only. It is **not** widened to RFC1918: Docker Desktop presents a
bridge-gateway peer address, and trusting all of RFC1918 would make that peer trusted and hand every
container-originated request full XFF authority.

> **TODO on first boot:** run `curl -s localhost:8000/debug/whoami` once, record the actual peer
> address observed on this machine below, and add exactly that `/32` if the demo requires it.
>
> Observed peer address on this host: _(not yet measured — fill in during Phase 2)_

---

## 8. Log event vocabulary

Logs are single-line JSON. Every line carries `ts`, `level`, `event`, and `request_id`.

The strings below are the **same vocabulary** used for metric label values and for the terms in
`ARCHITECTURE.md` / `RUNBOOK.md`. One concept, one word, everywhere — so a `grep` for a term in the
docs finds the log lines and the metrics too.

| Event | Level | When |
|---|---|---|
| `request` | INFO | One per completed request: region, attempts, status, duration. |
| `upstream_attempt_failed` | WARNING | A single region attempt failed. Carries `code`, `fclass`, `ms`. |
| `upstream_failover` | WARNING | **[BRIEF]** We caught the failure and are moving to the next region. Carries `from_region`, `to_region`, `error_class`, `error`, `elapsed_ms`. This is the line requirement #4 is graded on. |
| `all_regions_down` | ERROR | Every region failed; returning 503. |
| `region_skipped_breaker_open` | INFO | Resilient mode only: skipped *without* an attempt. Deliberately a different event from `upstream_failover` so "caught it" and "skipped it" can never be conflated in the report. |
| `circuit_opened` / `circuit_half_open` / `circuit_closed` | WARNING / INFO / INFO | Breaker transitions. |
| `rate_limit_rejected` | INFO | 429 returned. |
| `rate_limit_degraded` | WARNING | Redis unreachable; serving on the local fallback bucket. |

### Failure classes and codes

These strings cross every boundary: they appear in `X-Failover-Path`, in the `code` / `fclass`
fields of `upstream_attempt_failed`, and as the `code` label on `proxy_failover_total`.

- `fclass` ∈ **`NOT_SENT`** (no bytes reached the region; replay is free) |
  **`MAYBE_SENT`** (bytes may have arrived; replay is safe only because inference is deterministic).
- `code` ∈ `ok`, `connect_error`, `connect_timeout`, `read_timeout`, `pool_timeout`,
  `protocol_error`, `read_error`, `http_<status>` (e.g. `http_503`), `attempt_deadline`,
  `budget_exhausted`, `open_skipped`, `half_open_skipped`, `replay_blocked`.

---

## 9. Brief-fidelity invariants

These exist specifically so the project's added sophistication cannot obscure what the brief asked
for. They are not stylistic preferences.

1. **The three region containers are named exactly** `us-east-1-endpoint`, `eu-west-1-endpoint`,
   `ap-southeast-1-endpoint`, and a Redis container runs alongside them.
2. **Priority order is fixed**: `us-east-1` → `eu-west-1` → `ap-southeast-1`.
3. **The read timeout is 200 ms**, set via `httpx.Timeout(0.200, connect=0.100, pool=0.050)`, and
   every upstream call is inside a `try` / `except`.
4. **`MRFP_MODE=literal` is the default**, so the circuit breaker is off and *every* request during
   an outage actually enters the `except` block. There is no active health poller in this project at
   all — one would detect the outage before any data-plane request failed, and the `except` block
   would never fire.
5. **Chaos assertion A13** requires the count of `upstream_failover` log lines to be ≥ 95 % of the
   requests that failed over. If the except block is ever bypassed, `make chaos` fails.

---

## 10. Test database and safety rails

- pytest uses `redis://127.0.0.1:6380/15`.
- The fixture refuses to run unless `db == 15` and a container-written sentinel key is present.
- `FLUSHALL` appears nowhere in this repository.
- `tests/unit` must pass with the Docker daemon **stopped**. It uses `httpx.MockTransport` and never
  opens a socket.

---

## Change log

| Date | Change |
|---|---|
| 2026-07-27 | Initial freeze, before any application code. |
| 2026-08-07 | Post-review revision: headers scoped to 429 as well as 503; breaker env vars added; failure class/code vocabulary added (§8); `/admin/chaos` body schema added (§3). |
