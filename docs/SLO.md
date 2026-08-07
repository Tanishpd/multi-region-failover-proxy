# SLOs and capacity

Service level objectives, the alerts derived from them, and the measured
throughput ceiling. Rules live in `ops/observability/rules/slo.yml`; the alert
playbooks are [RUNBOOK §9](RUNBOOK.md#9-alerts).

---

## Objectives

| SLI | Objective | Measured from |
|---|---|---|
| **Availability** | 99.9% of `/v1/infer` requests succeed | `proxy_http_responses_total`, excluding `rate_limited` and `client_error` from the denominator — an abusive caller must not be able to burn the error budget |
| **Latency** | p99 under 3× the slowest region's base latency | `proxy_request_latency_seconds` |
| **Failover correctness** | 99.99% of requests whose primary failed are still served | `served_failover` vs `all_regions_down` |

Availability deliberately counts a fail-closed rate limiter as an outage: it
wears a 429, but it is Redis being down and rejecting everything.

---

## Capacity

Two ceilings, different in kind.

**CPU-bound throughput** — measured by `make capacity`, which steps the load up
until the sustained rate falls short of target or the admission gate sheds:

<!-- AUTOGEN:BEGIN:capacity -->
| Measured | 2026-08-07 |
|---|---|
| Knee (sustained, p99 healthy) | **200.2 rps** |
| p50 at the knee | 22.7 ms |
| p99 at the knee | 29.1 ms |
| First degraded step | 400 rps (p99 5578.6 ms) |

_The knee is the highest rate whose p99 stayed within 3x the lightest step's, not the highest rate the generator could push._

| target rps | achieved | p50 | p99 | shed |
|---|---|---|---|---|
| 50 | 50.2 | 27.2 ms | 38.5 ms | 0 |
| 100 | 100.2 | 24.0 ms | 28.9 ms | 0 |
| 200 | 200.2 | 22.7 ms | 29.1 ms | 0 |
| 400 | 376.6 | 25.1 ms | 5578.6 ms | 27 |
<!-- AUTOGEN:END -->

**What that number is actually measuring.** The mock regions are single-worker
uvicorn processes with a simulated `sleep`, so this knee describes *the weakest
link in this stack*, not the proxy. The proxy's own middleware ceiling was
measured separately at ~6,600 rps ([ADR-022](DECISIONS.md#adr-022)). Re-measure
on the deployment target before sizing a fleet from it — and expect real
upstreams, not the proxy, to set the ceiling there too.

Worth noting what the degraded step shows: at 400 rps the admission gate
**shed** rather than collapsing, which is the behaviour ADR-023 exists to
produce. Overload becomes a fast 503, not a fake region outage.

**Concurrency-bound throughput** — arithmetic, not benchmark. During a total
blackout every request costs the full failover budget, so by Little's Law:

```
throughput <= max_inflight / total_budget
```

Demo: `128 / 0.9s ≈ 142 rps`. Prod: `48 / 64s ≈ 0.75 rps per replica`.

That second number is the important one and no amount of CPU changes it: when
every region is down, a proxy's job is to fail fast and shed, not to sustain
throughput. It is also why `MRFPSelfInflictedSaturation` pages — reaching this
ceiling means something upstream is very wrong.

---

## Scale-out trigger

Scale when **either** holds for 10 minutes:

- `proxy_admission_inflight / proxy_max_inflight > 0.7`
- `proxy_region_pool_waiters > 0` (the leading indicator — it goes positive
  before any timeout fires)

Do **not** scale on CPU. The proxy is I/O-bound waiting on upstreams, so CPU
stays flat while latency degrades, and an HPA on CPU would never fire.
