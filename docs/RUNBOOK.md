# RUNBOOK

How to run, break, and diagnose this system. For *why* it works this way, see
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. Preconditions

Run `make doctor` first. It checks all of the following and prints an actionable fix for each:

| Check | Expected on this machine |
|---|---|
| Docker daemon reachable | Must be running — `open -a Docker` and wait ~30 s |
| Port 6379 | **Occupied by a native redis-server. This is expected**, not an error — ours publishes on 6380 |
| Ports 8000, 6380, 9001-9003 | Free |
| Architecture | `arm64` — never build with `platform: linux/amd64` |
| GNU Make version | 3.81 here; `doctor` warns because `.SHELLFLAGS` is silently ignored below 3.82 |
| Free disk | ≥ 3 GB. Images total ~600 MB, but builds need scratch space |
| `.env` present | `doctor` copies `.env.example` if missing |

---

## 2. Lifecycle

```bash
make build      # build both images. First run is 3-6 min; deliberately separate from `up`
make up         # start all five containers and wait for healthy
make ps         # what's running
make health     # one line per region: id, healthy?, breaker state, last error
make logs       # follow proxy logs (JSON)
make down       # stop and remove containers
make reset      # delete app rate-limit keys (db 0 only — never touches your native Redis)
```

`build` and `up` are separate on purpose: a cold build takes minutes, and a health-wait deadline
started at `up` time would expire during it and report a false failure.

---

## 3. Command reference

| Command | What it does |
|---|---|
| `make test-unit` | Unit tests. **Works with Docker stopped.** No sockets, no Redis. |
| `make test` | Full suite: unit + integration (needs Redis) + e2e (needs the stack). |
| `make chaos` | The brief's scenario end to end, with assertions. Exits non-zero on failure. |
| `make chaos-<name>` | One named scenario (§4). |
| `make report` | Regenerate the AUTOGEN blocks in docs/CHAOS-REPORT.md from `artifacts/latest/`. |
| `make bench` | ApacheBench throughput cross-check. Must use `-p` — `/v1/infer` is POST-only. |
| `make lint` / `make fmt` | ruff. |
| `make docs-check` | Fails if any measurement sits outside an `AUTOGEN` block, or a TODO remains. |

---

## 4. Scenario playbooks

Each scenario is `make chaos-<name>`, writes to `artifacts/<runid>/`, and symlinks
`artifacts/latest/`.

### `stop-us-east` — the brief

Floods the proxy, runs `docker stop us-east-1-endpoint` mid-flood, then restarts it and verifies
recovery.

**What you should see:** a one-second transition in the timeline, zero 5xx reaching the client, and
`upstream_failover` log lines with `error_class: ConnectError`.

**What it proves:** the `except` block catches a real connection failure and routes onward.
**What it cannot prove:** that the 200 ms timeout works — this failure mode never reaches it.

### `pause-us-east` — the one that proves the timeout

`docker pause` freezes the process without closing its sockets: connect succeeds, the read hangs,
and the 200 ms timeout fires (mechanism:
[LEARNING §3](LEARNING.md#3-failure-modes-are-not-interchangeable)).

**What you should see:** transition p50 around 200 ms instead of ~2 ms, and `error_class: ReadTimeout`.

**Run both and compare** — two failure modes, visibly different latency signatures, one code path.

### `cascade-two` — proves it's a loop, not an if/else

Stops `us-east-1` *and* `eu-west-1`. Traffic must reach `ap-southeast-1`.

### `total-blackout` — the 503 contract

Stops all three. Expect a clean `503` with `X-Failover-Path` listing all three attempts, `Retry-After`
set, and the proxy still answering `/healthz`. Degrading is not crashing.

### `recover-us-east` — breaker half-open

Runs in `resilient` mode. Look for `circuit_opened` → `circuit_half_open` → `circuit_closed`, and
traffic returning to the primary.

### `rate-limit-storm` — requirement 2 end to end

Exceeds the bucket deliberately and asserts the 429 rate, the `Retry-After` value, and that the
bucket refills at the configured rate.

### Manual scenarios (not automated)

| Scenario | Command | Induces |
|---|---|---|
| Abrupt kill | `docker kill us-east-1-endpoint` | RST on pooled sockets → `RemoteProtocolError` |
| Network partition | `docker network disconnect mrfp-net us-east-1-endpoint` | DNS record withdrawn → fast `ConnectError`, same signature as `stop` |
| Slow region | `curl -XPOST localhost:9001/admin/chaos -H 'X-Admin-Token: dev-chaos' -d '{"mode":"slow","ms":500}'` | Latency without failure |
| Region 5xx | same, `{"mode":"error"}` | Response-level failure, not transport |
| Blackhole | same, `{"mode":"blackhole"}` | `ReadTimeout` without pausing the container |
| Redis down | `docker stop redis` | Rate limiter fail-open path; watch `X-RateLimit-Mode` go `redis` → `local` |
| Proxy restart | `docker restart mrfp-proxy` | Cold pools, empty script cache → `NOSCRIPT` recovery |

Clear a chaos mode with `curl -XDELETE localhost:9001/admin/chaos -H 'X-Admin-Token: dev-chaos'`.

---

## 5. Reading the logs

Logs are single-line JSON. Useful recipes:

```bash
# The failover moment — the line requirement #4 is graded on
docker compose logs --no-log-prefix proxy | jq -c 'select(.event=="upstream_failover")'

# Which exception actually fired, counted
docker compose logs --no-log-prefix proxy \
  | jq -r 'select(.event=="upstream_failover") | .error_class' | sort | uniq -c

# Everything about one request
docker compose logs --no-log-prefix proxy | jq -c 'select(.request_id=="8f2c1d…")'

# Breaker transitions in order
docker compose logs --no-log-prefix proxy | jq -c 'select(.event|startswith("circuit_"))'

# Rate limiting: rejections vs degraded-mode decisions
docker compose logs --no-log-prefix proxy \
  | jq -c 'select(.event|startswith("rate_limit_"))'
```

**`upstream_failover`** = attempted and caught (the graded behaviour);
**`region_skipped_breaker_open`** = never attempted ([ADR-009](DECISIONS.md#adr-009)).

---

## 6. Metrics

`curl -s localhost:8000/metrics`

| Metric | Use |
|---|---|
| `proxy_requests_total{region,status}` | Traffic split across regions |
| `proxy_failover_total{from_region,to_region,code}` | Did failover happen, and why |
| `proxy_upstream_latency_seconds` | Bucket edges straddle 0.2 so the timeout is visible |
| `proxy_circuit_state{region}` | 0 closed / 1 half-open / 2 open |
| `rate_limit_rejected_total` | 429 count |
| `rate_limit_mode_total{mode}` | redis / local / open |
| `rate_limit_noscript_recoveries_total` | Should be near zero except after a Redis restart |

---

## 7. Troubleshooting

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `Cannot connect to the Docker daemon` | Desktop not running | `docker info` | `open -a Docker`, wait, `make doctor` |
| Redis container fails to bind | Native redis on 6379 | `lsof -nP -iTCP:6379 -sTCP:LISTEN` | Nothing — ours is on 6380 by design |
| Every client shares one bucket | Proxy sees the bridge gateway as the peer | `curl localhost:8000/debug/whoami` | Confirm `MRFP_TRUSTED_PROXIES`; add the observed peer `/32` only if needed |
| Unexpected 429s during chaos | The flood is hitting the real limiter | `X-RateLimit-Mode` header | Set `MRFP_CHAOS_MODE=1` and use the bypass token |
| Latency stays high for the whole outage | Expected in `literal` mode — that's the tax the breaker removes | `X-Breaker-State: n/a` | Re-run with `MRFP_MODE=resilient` |
| Breaker never opens | You're in `literal` mode (the default) | `grep region_skipped_breaker_open` returns nothing | Intentional. `MRFP_MODE=resilient` to enable |
| A region "won't die" during chaos | uvicorn graceful shutdown waiting on keep-alive sockets | Time `docker stop` — should be < 1.5 s | Confirm `--timeout-graceful-shutdown 0` and `stop_grace_period: 2s` |
| Region resurrects itself mid-run | A `restart:` policy plus a region *crash* (policies don't fire after `docker stop`), or Docker Desktop restarted with `always` set | `docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' us-east-1-endpoint` | Must be `no` |
| `make chaos` reports PASS but output looked wrong | GNU Make 3.81 ignores `.SHELLFLAGS`, so a failing pipeline exits 0 | `make --version` | Recipes must be one command delegating to `ops/scripts/*.sh` |
| Healthchecks never go healthy | A healthcheck using `curl` — `python:slim` doesn't have it | `docker inspect --format '{{json .State.Health}}' <name>` | Use the `python -c urllib.request` form |
| Timings flap wildly | Image built for the wrong architecture | `docker image inspect mrfp/proxy:dev -f '{{.Architecture}}'` | Rebuild; never set `platform: linux/amd64` |
| Rate limiter allows more than capacity under load | Non-atomic path being used | `rate_limit_mode_total{mode="local"}` rising | The local fallback is per-process and approximate — check why Redis is unreachable |
| `docker compose` complains about the project name | Directory has spaces and `&` | `docker compose config --format json \| jq .name` | Must be `mrfp` via the top-level `name:` key |

---

## 8. Safety notes

This machine runs a **real Redis on 6379** with real data. Guardrails:

- The container publishes on **6380**. `MRFP_REDIS_URL` has no default, so a misconfiguration fails
  loudly instead of connecting to your Redis.
- Tests use **db 15** and refuse to run unless the db index is 15 *and* a sentinel key written by our
  own container is present.
- `FLUSHALL` appears nowhere in this repository. `make reset` scopes to db 0 and to the `rl:v1:*`
  key prefix.

The chaos harness pushes an inverse action onto an undo stack for every container it touches, drained
in a `finally` block and on SIGINT — Ctrl-C cannot leave a region stopped or paused. If it ever does,
`make up` restores everything.


---

## 9. Alerts

Five alerts page; everything else is a ticket. Each has a section here — and
`make docs-check` fails if an alert has no matching anchor, because an alert
that wakes someone and then tells them nothing is worse than no alert.

Rules live in `ops/observability/rules/slo.yml`.

### alert-MRFPAvailabilityFastBurn

Clients are getting 5xx from `/v1/infer` fast enough to exhaust a 30-day error
budget in about two days.

1. `curl -s localhost:8000/readyz | jq` — which regions does the proxy believe
   are usable? If none, this is an upstream outage, not a proxy problem.
2. `docker compose logs --no-log-prefix mrfp-proxy | jq -c 'select(.event=="all_regions_down")' | tail`
   — the `X-Failover-Path` in each shows what every region did.
3. If one region is dragging, remove it: set `MRFP_REGION_PRIORITY` to the
   remaining two and roll. Failover cannot help when every region is failing.

### alert-MRFPAvailabilitySlowBurn

Same signal, slower. Not an emergency; find the pattern before mitigating.
Check whether the failures correlate with one region, one tenant, or one
deploy — `proxy_http_responses_total` is labelled by outcome, and the structured
log carries `request_id` for joins.

### alert-MRFPSelfInflictedSaturation

**The proxy is the problem, not the regions.** `pool_timeout` and
`attempt_deadline` cannot be produced by a region outage; they are generated in
this process when it runs out of connections or its own deadline fires.

1. `curl -s localhost:8000/metrics | grep -E 'admission_inflight|pool_waiters'`
2. If `proxy_admission_inflight` is pinned at the limit, you are load-shedding
   correctly and need more replicas.
3. If `proxy_region_pool_waiters` is positive while admission has headroom, the
   per-region pool is the bottleneck — raise `client_defaults.max_connections`
   in `config/regions.yaml`.
4. **Do not** conclude a region is down. The breaker deliberately ignores these
   codes for exactly this reason; if you see a breaker open at the same time,
   something reintroduced the feedback loop.

### alert-MRFPFailoverNotHappening

The primary is failing and nothing is failing over — the mechanism this whole
system exists for has regressed.

1. Which mode? `curl -s -H "Authorization: Bearer $MRFP_ADMIN_TOKEN" localhost:8000/admin/regions | jq .mode`
2. In `resilient` mode, check whether breakers are open on every region: that
   is not a failure to fail over, it is nothing left to fail over to.
3. `grep -c upstream_failover` in the proxy logs. Zero with a failing primary
   means the except block is being bypassed — the exact regression chaos
   assertion A13 guards against. Run `make chaos` to reproduce.

### alert-MRFPMetricsAbsent

The scrape is gone, so every other rule silently stopped evaluating. Treat as
an outage of your ability to detect outages. Check the proxy is up
(`/healthz`), that `MRFP_METRICS_PUBLIC` was not set to `0` without giving the
scraper a token, and that the scrape target still resolves.

### alert-MRFPPoolQueueing

Leading indicator, ticket severity. Requests are waiting for a connection but
nothing has timed out yet. Scale out, or raise the per-region pool, before this
becomes `MRFPSelfInflictedSaturation`.

### alert-mrfpratelimitdegraded

**Means:** the limiter is not using Redis. Every replica is enforcing its own
in-process bucket, so the fleet total is roughly `replicas x limit` divided by
however unevenly traffic is balanced. Requests still succeed — this is a
correctness problem, not an availability one.

**Check**

```
curl -s localhost:8000/readyz | jq .rate_limit
docker compose logs --no-log-prefix mrfp-proxy | jq -c 'select(.event=="rate_limit_degraded")' | tail -5
```

The `reason` field is the whole diagnosis:

| reason | meaning | action |
|---|---|---|
| `unreachable` | Redis is down or unroutable | this is what fail-open is for; restore Redis |
| `auth` | Redis is up and rejecting us | see [alert-mrfpratelimitrefused](#alert-mrfpratelimitrefused) |
| `protocol` | Redis rejected the command itself | our script or its arguments; check for a partial deploy |
| `other` | unclassified | read `error_class`; it belongs in one of the above |

**Fix:** restore Redis. The limiter reattaches on its own — no restart needed,
and `rate_limit_backend_up` returns to 1 on the first successful decision.

### alert-mrfpratelimitrefused

**Means:** Redis is answering and refusing us. **This will not recover on its
own**, and unlike an outage it is not what fail-open was designed for: the
service is serving unlimited traffic while every dashboard reports it healthy.

**Cause, in order of likelihood**

1. `MRFP_REDIS_PASSWORD` not reaching the proxy while Redis runs
   `--requirepass`. This shipped once — the prod compose set the password on the
   Redis container only ([ADR-029](DECISIONS.md#adr-029)).
2. A rotated credential the proxy has not been redeployed with.
3. An ACL on the account that does not permit `EVALSHA`.

**Check**

```
docker compose logs --no-log-prefix mrfp-proxy | jq -c 'select(.event=="redis_verified")'
docker compose exec redis redis-cli -a "$MRFP_REDIS_PASSWORD" ping
```

**Note:** a *fresh* deploy cannot reach this state — the proxy verifies Redis at
boot and refuses to start when it is refused. Seeing this alert means the
credential broke *after* a successful start, so the fix is a redeploy with the
current secret rather than a config edit.


## Drill: a Redis restart under live traffic

The NOSCRIPT path. After a restart or `SCRIPT FLUSH` the script cache is empty,
so `EVALSHA` fails and every in-flight request discovers it at once. The
generation counter in `ratelimit/scripts.py` exists so exactly ONE caller
re-uploads while the rest retry the cheap `EVALSHA` — the naive alternatives
being N concurrent full-body `EVAL`s or serialising every request behind one
lock.

```
( sleep 8; docker restart redis ) &
PROBE_URL=http://127.0.0.1:8000/v1/infer PROBE_RPS=10 PROBE_DURATION_S=25 \
  .venv/bin/python ops/drills/proxy_probe.py
curl -s localhost:8000/metrics | grep -E 'noscript_recoveries|backend_up'
```

Measured on this stack:

| what | result |
|---|---|
| client-visible failures | **0 of 250** — fail-open held |
| limiter modes | 246 `redis`, 4 `local` during the gap |
| `rate_limit_noscript_recoveries_total` | 0 → **1** — one EVAL, not one per request |
| `rate_limit_degraded` reason | `unreachable` (an outage, correctly not `auth`) |
| `rate_limit_backend_up` | back to **1.0**, no restart needed |

A run showing 0 recoveries means the restart never emptied the cache and the
path was not exercised; a run showing one per request means the generation
counter has regressed.
