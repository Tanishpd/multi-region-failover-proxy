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
