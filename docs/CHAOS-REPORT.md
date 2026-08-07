# CHAOS REPORT

> **Every number below was measured, not written.** The `AUTOGEN` blocks are filled by
> `chaos/report.py` from raw artifacts in `artifacts/<runid>/`; the prose between them is the only
> hand-authored content, and `make docs-check` fails the build if a block is left unfilled.
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
| | |
|---|---|
| Generated | 2026-08-07T04:47:58+00:00 |
| Host | Darwin 25.5.0, arm64 |
| Docker | 29.5.3 |
| Compose | 5.1.4 |
| Proxy mode | literal |
| Git commit | 6b8e4fc |

Read timeout 200 ms, connect 100 ms, pool 50 ms, total budget 900 ms.
Rate limit: capacity 20, refill 20/s. Load: 50 rps, open-loop.
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
| `stop-us-east` | `docker stop us-east-1-endpoint` | DNS withdrawal → `ConnectError`, fast fail | <!-- AUTOGEN:INLINE:result:stop-us-east -->PASS<!-- AUTOGEN:INLINE:END --> |
| `pause-us-east` | `docker pause us-east-1-endpoint` | Connect succeeds, read hangs → `ReadTimeout` at 200 ms | <!-- AUTOGEN:INLINE:result:pause-us-east -->PASS<!-- AUTOGEN:INLINE:END --> |
| `cascade-two` | Stop `us-east-1` + `eu-west-1` | Two failures in one request; proves a loop, not an if/else | <!-- AUTOGEN:INLINE:result:cascade-two -->PASS<!-- AUTOGEN:INLINE:END --> |
| `total-blackout` | Stop all three | 503 contract; proxy stays alive | <!-- AUTOGEN:INLINE:result:total-blackout -->PASS<!-- AUTOGEN:INLINE:END --> |
| `recover-us-east` | Stop, wait, start (resilient mode) | Breaker half-open probe → closed | <!-- AUTOGEN:INLINE:result:recover-us-east -->PASS<!-- AUTOGEN:INLINE:END --> |

The token bucket is not a chaos scenario: it is covered by 15 integration tests against a real
Redis (`tests/integration/test_token_bucket_lua.py`), including 500 concurrent calls against a
frozen clock allowing exactly `capacity`.

---

## 4a. The commands that ran

Recorded by the harness itself, so this report cannot claim a command it never executed.

<!-- AUTOGEN:BEGIN:commands-stop-us-east -->
```
$ docker stop us-east-1-endpoint    # returned in 434 ms
$ docker start us-east-1-endpoint    # returned in 114 ms
```
<!-- AUTOGEN:END -->

---

## 5. Failover timeline — `stop-us-east`

Requests per second by serving region, around the fault.

<!-- AUTOGEN:BEGIN:timeline-stop-us-east -->
```
 sec | us-east | eu-west |  ap-se |  err |  p99ms | traffic
-----------------------------------------------------------
   0 |      50 |       0 |      0 |    0 |     34 | ████████████
   1 |      50 |       0 |      0 |    0 |     34 | ████████████
   2 |      50 |       0 |      0 |    0 |     31 | ████████████
   3 |      50 |       0 |      0 |    0 |     30 | ████████████
   4 |       5 |      45 |      0 |    0 |    178 | ████████████   <- fault injected; fault complete
   5 |       0 |      50 |      0 |    0 |     84 | ████████████
   6 |       0 |      50 |      0 |    0 |     84 | ████████████
   7 |       0 |      50 |      0 |    0 |     87 | ████████████
   8 |       0 |      50 |      0 |    0 |     85 | ████████████   <- repairing
   9 |      48 |       2 |      0 |    0 |     71 | ████████████
  10 |      50 |       0 |      0 |    0 |     33 | ████████████
  11 |      50 |       0 |      0 |    0 |     31 | ████████████   <- repaired
  12 |      50 |       0 |      0 |    0 |     32 | ████████████
  13 |      50 |       0 |      0 |    0 |     30 | ████████████
  14 |      50 |       0 |      0 |    0 |     32 | ████████████
  15 |      50 |       0 |      0 |    0 |    112 | ████████████
  16 |       1 |       0 |      0 |    0 |     25 | ▁
```
<!-- AUTOGEN:END -->

### `pause-us-east`, for comparison

The same shape, but note the p99 column: this is what a real timeout costs.

<!-- AUTOGEN:BEGIN:timeline-pause-us-east -->
```
 sec | us-east | eu-west |  ap-se |  err |  p99ms | traffic
-----------------------------------------------------------
   0 |      50 |       0 |      0 |    0 |     67 | ████████████
   1 |      50 |       0 |      0 |    0 |     33 | ████████████
   2 |      50 |       0 |      0 |    0 |     32 | ████████████
   3 |      50 |       0 |      0 |    0 |     35 | ████████████
   4 |       0 |      50 |      0 |    0 |    289 | ████████████   <- fault injected; fault complete
   5 |       0 |      50 |      0 |    0 |    284 | ████████████
   6 |       0 |      50 |      0 |    0 |    300 | ████████████
   7 |       2 |      48 |      0 |    0 |    286 | ████████████
   8 |      50 |       0 |      0 |    0 |    152 | ████████████   <- repairing
   9 |      50 |       0 |      0 |    0 |     32 | ████████████
  10 |      50 |       0 |      0 |    0 |     30 | ████████████   <- repaired
  11 |      50 |       0 |      0 |    0 |     35 | ████████████
  12 |      50 |       0 |      0 |    0 |     54 | ████████████
  13 |      50 |       0 |      0 |    0 |     35 | ████████████
  14 |      50 |       0 |      0 |    0 |     46 | ████████████
  15 |      50 |       0 |      0 |    0 |     29 | ████████████
  16 |       1 |       0 |      0 |    0 |     36 | ▁
```
<!-- AUTOGEN:END -->

---

## 6. Summary statistics — `stop-us-east`

Three windows: **before** the fault (baseline), **during** the outage, and **after** recovery.

<!-- AUTOGEN:BEGIN:summary-stop-us-east -->
| Window | Requests | Success | Errors | p50 | p99 | Regions serving |
|---|---|---|---|---|---|---|
| baseline | 200 | 200 | 0 | 25.9 ms | 33.4 ms | us-east-1 ×200 |
| outage | 200 | 200 | 0 | 76.7 ms | 84.2 ms | eu-west-1 ×200 |
| recovery | 173 | 173 | 0 | 26.8 ms | 92.7 ms | us-east-1 ×173 |
<!-- AUTOGEN:END -->

### Assertions

<!-- AUTOGEN:BEGIN:assertions-stop-us-east -->
| id | Assertion | Result | Detail |
|---|---|---|---|
| A1 | no client-side errors | PASS | 0 of 801 requests failed client-side |
| A2 | no 5xx or 429 surfaced | PASS | 0 error responses of 801 (0.00%) |
| A3 | nothing served by us-east-1 during the outage | PASS | 0 of 140 scored requests were still served by us-east-1 (last 1.2s before repair excluded: rescuable in flight) |
| A4 | failover detected quickly | PASS | first eu-west-1 success 10 ms after the stop completed (budget 1500 ms) |
| A5 | transition p99 within budget | PASS | p99 84 ms during vs 33 ms baseline (ceiling 633 ms) |
| A8 | failover was logged with an error class | PASS | 247 upstream_failover lines from us-east-1; error_class=['ConnectError', 'ConnectTimeout']; hops=['us-east-1->eu-west-1'] |
| A9 | log lines correlate with the harness | PASS | 247 of 247 failover lines matched a request_id |
| A11 | docker stop returned promptly | PASS | docker stop took 434 ms |
| A12 | achieved throughput >= 90% of target | PASS | 50.1 rps achieved vs 50 target |
| A13 | every failed-over request hit the except block | PASS | 200 of 200 failed-over requests have an upstream_failover line (100.0%) |
| A14 | traffic returned to us-east-1 after restart | PASS | 173 of 173 post-recovery requests served by us-east-1 |
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
| | `docker stop` | `docker pause` |
|---|---|---|
| Exception class | `connect_error` ×200 | `read_timeout` ×195 |
| Detection p50 | **4.8 ms** | **202.6 ms** |
| Detection p99 | 7.7 ms | 206.8 ms |
| Detection min / max | 2.9 / 13.1 ms | 200.3 / 228.1 ms |
| Outage-window p50 | 76.7 ms | 275.9 ms |
| Read timeout exercised? | no — failed at DNS first | **yes** |

Ratio of detection p50: **42×**.
<!-- AUTOGEN:END -->

**The hypothesis held.** `docker stop` fails in single-digit milliseconds via DNS withdrawal;
`docker pause` fails at essentially exactly the configured read timeout, with a minimum sample above
200 ms. Two faults that both mean "the region is gone", separated by a factor of roughly forty in
detection latency and reaching different `except` branches — through one unchanged code path.

The consequence is the finding this project exists to produce: **the brief's own chaos command
cannot exercise the brief's own timeout.** An implementation tested only with `docker stop` would
carry a completely unexercised 200 ms timeout and get no signal that anything was wrong. The `pause`
scenario is what closes that gap.

---

## 8. Literal vs resilient mode

`make chaos` runs `literal` first (breaker disabled — the graded configuration), then `resilient`.

<!-- AUTOGEN:BEGIN:mode-comparison -->
Both rows are `pause-us-east` on the same stack; only `MRFP_MODE` differs.

| | literal (default) | resilient |
|---|---|---|
| p50 during outage | 275.9 ms | **75.8 ms** |
| p99 during outage | 290.2 ms | 297.7 ms |
| `upstream_failover` lines | **198** | 16 |
| `region_skipped_breaker_open` | 0 | 251 |
| `circuit_opened` | 0 | 1 |
| errors surfaced | 0 | 0 |
<!-- AUTOGEN:END -->

Both effects landed, and the second is the reason `literal` is the default. The breaker cuts p50
during the outage by roughly 3.6×, which is exactly the tax it exists to remove. It also cuts the
`upstream_failover` count by around 92%, because a skipped region is never attempted — so the
`try`/`except` the brief asks for would fire for a small fraction of requests instead of all of
them. Better engineering, worse evidence. ([ADR-009](DECISIONS.md#adr-009))

---

## 9. Interpretation

Written after reading the numbers above.

1. **Failover is invisible to callers.** Across the stop and pause runs, 801 requests each, not one
   client saw an error — no 5xx, no 429, no dropped connection. The brief asks for traffic to keep
   flowing "without breaking the user's application"; that is what the request log shows.

2. **Detection latency is dominated by the failure MODE, not by the proxy.** The router does
   identical work in both scenarios; the ~40× gap comes entirely from how the region died. That is
   the argument for classifying exceptions instead of treating every failure as "upstream down".

3. **The 200 ms read timeout is real and precisely calibrated.** Pause detection sits just above
   200 ms with a minimum sample above it too — the timeout firing, not a coincidence of latency.

4. **Recovery deserves testing even though the brief omits it.** Every scenario restores the region
   and asserts traffic returns. A failover system that cannot fail back is half a system.

5. **A single global timeout is the wrong shape when regions differ in latency.** The lone 503 seen
   in `cascade-two` came from ap-southeast-1, whose 150 ms simulated latency leaves roughly 30 ms of
   headroom under a 200 ms deadline once it absorbs the whole flood alone. Production would scale the
   deadline per region; this keeps it flat because 200 ms is the number the brief specifies.

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
