# CHAOS REPORT

> **No run has been executed yet.** Every `AUTOGEN` block below is empty and will be filled by
> `chaos/report.py` from the raw artifacts in `artifacts/<runid>/`. Nothing in this file is written by
> hand except the prose — if you see a number here, a script put it there.
>
> Regenerate with: `make chaos && make report`

`chaos/report.py` rewrites **only** two marker forms: block regions between
`<!-- AUTOGEN:BEGIN:x -->` and `<!-- AUTOGEN:END -->`, and single-line spans between
`<!-- AUTOGEN:INLINE:x -->` and `<!-- AUTOGEN:INLINE:END -->` — the inline form has a distinct
terminator so a block-scanning regex can never pair across an inline span. Prose outside the markers
survives regeneration. `make docs-check` fails the build if a measurement appears outside an autogen
region.

---

## 1. Hypothesis

Stated before the first run, so the result can contradict it.

> When `us-east-1-endpoint` is stopped during a sustained request flood, the proxy will detect the
> failure on the first affected request, log it, and route to `eu-west-1` **without surfacing a single
> error to the client**. Steady-state latency will rise by roughly the failure-detection cost for the
> duration of the outage (in `literal` mode, where the circuit breaker is disabled), and will return to
> baseline within seconds of the region being restarted.
>
> Secondary hypothesis: the failure will present as a **connection error in single-digit
> milliseconds**, not as a timeout — because `docker stop` causes Docker's embedded DNS resolver to
> withdraw the container's record. The 200 ms read timeout will therefore **not** be exercised by this
> scenario, and will require `docker pause` to test.

---

## 2. Environment

<!-- AUTOGEN:BEGIN:environment -->
_pending — populated from the run's `summary.json`_
<!-- AUTOGEN:END -->

---

## 3. Methodology

The load generator is an **open-loop** pacer: requests are issued on a fixed schedule
(`next_at += 1/rps`) whether or not earlier ones have returned. A closed-loop worker pool stops
issuing while blocked, which deletes the failover latency spike from the dataset — the coordinated
omission problem. See [ARCHITECTURE §9](ARCHITECTURE.md#9-measurement-methodology).

- Each request records: send time (monotonic), receive time, status, latency, serving region (from
  `X-Served-By-Region`), attempt count, and a client-generated `X-Request-Id`.
- Client-side timeout is 5 s — roughly 25× the proxy's 200 ms — so a client abort can never be
  misattributed to the proxy.
- Fault injection uses `create_subprocess_exec` with an argv list. The literal command string is
  recorded in `summary.json` as `command_executed`, so the report proves what actually ran.
- Assertions about *when* a request was routed are evaluated on **send** time. A request dispatched to
  a healthy region microseconds before the kill can land after it; keying on receive time would fail
  the assertion for correct behaviour.
- Baseline percentiles are measured **within the same run**, not hard-coded. Absolute thresholds flake
  across machines and across cold/warm caches.

---

## 4. Scenario matrix

| Scenario | Fault | Code path exercised | Result |
|---|---|---|---|
| `stop-us-east` | `docker stop us-east-1-endpoint` | DNS withdrawal → `ConnectError`, fast fail | <!-- AUTOGEN:INLINE:result:stop-us-east -->_pending_<!-- AUTOGEN:INLINE:END --> |
| `pause-us-east` | `docker pause us-east-1-endpoint` | Connect succeeds, read hangs → `ReadTimeout` at 200 ms | <!-- AUTOGEN:INLINE:result:pause-us-east -->_pending_<!-- AUTOGEN:INLINE:END --> |
| `cascade-two` | Stop `us-east-1` + `eu-west-1` | Two failures in one request; proves a loop, not an if/else | <!-- AUTOGEN:INLINE:result:cascade-two -->_pending_<!-- AUTOGEN:INLINE:END --> |
| `total-blackout` | Stop all three | 503 contract; proxy stays alive | <!-- AUTOGEN:INLINE:result:total-blackout -->_pending_<!-- AUTOGEN:INLINE:END --> |
| `recover-us-east` | Stop, wait, start (resilient mode) | Breaker half-open probe → closed | <!-- AUTOGEN:INLINE:result:recover-us-east -->_pending_<!-- AUTOGEN:INLINE:END --> |
| `rate-limit-storm` | Exceed the bucket | Token bucket denial and refill | <!-- AUTOGEN:INLINE:result:rate-limit-storm -->_pending_<!-- AUTOGEN:INLINE:END --> |

---

## 5. Failover timeline — `stop-us-east`

Requests per second by serving region, around the fault.

<!-- AUTOGEN:BEGIN:timeline-stop-us-east -->
_pending — populated from `artifacts/latest/timeline.txt`_

```
sec | us-east | eu-west | ap-se | 5xx | p99ms | bar
    |         |         |       |     |       |
```
<!-- AUTOGEN:END -->

---

## 6. Summary statistics — `stop-us-east`

Three windows: **before** the fault (baseline), **during** the outage, and **after** recovery.

<!-- AUTOGEN:BEGIN:summary-stop-us-east -->
_pending — populated from `artifacts/latest/summary.json`_

| Window | Requests | Success | 5xx | p50 | p99 | Regions serving |
|---|---|---|---|---|---|---|
| before | | | | | | |
| during | | | | | | |
| after | | | | | | |
<!-- AUTOGEN:END -->

### Assertions

<!-- AUTOGEN:BEGIN:assertions-stop-us-east -->
_pending_

| id | Assertion | Result |
|---|---|---|
| A1 | Zero client-side errors | |
| A2 | Zero 5xx, zero 429 surfaced to the client | |
| A3 | Zero requests **served by** us-east-1 during the outage (send-time keyed; in literal mode every request is still *attempted* against it — that's A13) | |
| A4 | First successful eu-west-1 response within 1500 ms of the stop completing | |
| A5 | Transition p99 ≤ baseline p99 + 3 × 200 ms | |
| A8 | ≥1 `upstream_failover` log line with a non-empty `error_class` | |
| A9 | That line's `request_id` appears in `requests.csv` | |
| A10 | `proxy_failover_total{from_region="us-east-1"}` increased | |
| A11 | `docker stop` completed in < 1500 ms | |
| A12 | Achieved RPS ≥ 90 % of target | |
| A13 | `upstream_failover` lines ≥ 95 % of requests that failed over | |
<!-- AUTOGEN:END -->

_Assertion ids are global across the scenario matrix: A6 and A7 belong to the `pause-us-east` and
`cascade-two` scenarios and appear in their sections of the generated per-run report._

**On A13.** This is the assertion that protects the project brief from its own sophistication. If a
future change causes regions to be skipped (breaker) rather than attempted, the `try`/`except` stops
firing and this assertion fails the run. See [ADR-009](DECISIONS.md#adr-009).

---

## 7. `stop` vs `pause` — the comparison that matters

The same `except` clause, two fault injections, two entirely different latency signatures.

<!-- AUTOGEN:BEGIN:stop-vs-pause -->
_pending_

| | `docker stop` | `docker pause` |
|---|---|---|
| Exception class | | |
| Detection latency (p50) | | |
| Transition p50 | | |
| Read timeout exercised? | | |
<!-- AUTOGEN:END -->

The expectation — to be confirmed or refuted by the run — is that `stop` fails in single-digit
milliseconds via DNS withdrawal while `pause` fails at approximately the configured 200 ms read
timeout. If that holds, it demonstrates that **the brief's own chaos command cannot exercise the
brief's own timeout**, which is the single most useful finding this project can produce.

---

## 8. Literal vs resilient mode

`make chaos` runs `literal` first (breaker disabled — the graded configuration), then `resilient`.

<!-- AUTOGEN:BEGIN:mode-comparison -->
_pending_

| | literal | resilient |
|---|---|---|
| p50 during outage | | |
| p99 during outage | | |
| `upstream_failover` log lines | | |
| `region_skipped_breaker_open` log lines | | |
<!-- AUTOGEN:END -->

The breaker should reduce latency during the outage — that's what it's for. It should also reduce
the `upstream_failover` count, because skipped regions are never attempted
([ADR-009](DECISIONS.md#adr-009)).

---

## 9. Interpretation

<!-- AUTOGEN:BEGIN:interpretation-stats -->
_pending — key figures inlined here by the generator_
<!-- AUTOGEN:END -->

_Written by hand after reading the numbers above. Left empty until there are numbers to read._

---

## 10. Reproduction

```bash
make doctor && make build && make up
make chaos                    # writes artifacts/<runid>/ and symlinks artifacts/latest
make report                   # regenerates the AUTOGEN blocks in this file
```

Raw artifacts per run:

| File | Contents |
|---|---|
| `requests.csv` | One row per request: send/recv time, status, latency, region, attempts, request id |
| `summary.json` | Windowed statistics, assertion results, `command_executed`, environment |
| `timeline.txt` | The per-second ASCII table |
| `proxy-logs.jsonl` | Proxy logs captured for the run window |
| `report.md` | Standalone per-run report |
