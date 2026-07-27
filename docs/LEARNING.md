# LEARNING

The concepts behind this project, explained properly. Read this if you want to understand *why* the
code looks like it does, rather than just what it does.

---

## 1. Token buckets, and why not a fixed window

The simplest rate limiter is a counter that resets every second: `INCR key`, `EXPIRE key 1`, reject
above N. It has a flaw that shows up immediately under real traffic — the **boundary burst**.

With a limit of 20/second, a client sends 20 requests at 11:59:59.99 and 20 more at 12:00:00.01. Both
windows are individually legal. The server just took 40 requests in 20 milliseconds.

A token bucket has no boundaries. It holds up to `capacity` tokens, refilling continuously at
`refill_rate` per second. Each request spends one. When the bucket is empty, you wait.

```
capacity     = 20     # the largest burst allowed
refill_rate  = 20/s   # the sustained rate allowed
```

Those two numbers separate *burstiness* from *throughput*, which a fixed window cannot express at all.

### Lazy refill

You could run a background job topping every bucket up 20 times a second. You shouldn't — it's a
scheduler that can fail, and it costs work proportional to idle clients.

Instead, compute the refill when you look:

```
elapsed = now - last_seen
tokens  = min(capacity, tokens + elapsed × refill_rate)
```

Idle buckets cost nothing. There is no background job to break.

### Four details that are each a real bug

**Persist the fractional part.** At 20 tokens/s, 10 ms of elapsed time accrues 0.2 tokens. If you
store integers, that floors to 0 — and a client polling every 10 ms never refills *at all*, forever.
The bucket is permanently empty. This one is subtle enough that it usually ships.

**Clamp at capacity.** Without the `min()`, an hour of idleness accrues 72,000 tokens and your limit
means nothing on the next burst.

**TTL ≥ full refill time.** Buckets need to expire or idle clients leak memory. But if the key expires
*before* the bucket would have refilled completely, the next request finds no key, assumes a fresh
full bucket, and a client pacing just slower than the TTL gets unlimited capacity for free. The TTL
must be at least `capacity / refill_rate`.

**Refresh the TTL on denial too.** Otherwise a continuously rate-limited client lets its key expire
and returns with a full bucket — being blocked would *reward* it.

### Why it has to be atomic

```
GET tokens   -> 5        GET tokens   -> 5
compute 4                compute 4
SET tokens 4             SET tokens 4
```

Two requests served, one token spent. This is a classic lost update, and under any real concurrency it
happens constantly. A `WATCH`/`MULTI` retry loop works but adds round trips and retries under exactly
the load where you can least afford them.

Redis executes a Lua script single-threaded, start to finish, with nothing interleaved. Read, refill,
decide, debit, set the TTL — one round trip, atomic by construction. That's the whole reason for the
`.lua` file.

Two Lua-specific traps worth knowing: Lua→Redis conversion **truncates floats**, so return integers
(this code returns tokens × 1000). And Lua 5.1's `tostring` formats with `%.14g`, so a 13-digit
millisecond timestamp becomes `1.7532e+12` — use `string.format('%.0f', ms)`.

---

## 2. Timeouts are four different things

`timeout=0.2` looks like one number. In httpx it sets four:

| Timeout | What it bounds |
|---|---|
| **connect** | Establishing the TCP connection (and TLS) |
| **read** | The gap between bytes *while receiving* |
| **write** | The gap between bytes while sending |
| **pool** | Waiting for a free connection from the pool |

The one people get wrong is **read**. It is not a deadline for the response — it is a *gap timer*, reset
every time a byte arrives. A server that dribbles one byte every 199 ms never trips a 200 ms read
timeout, and can hold the connection open indefinitely.

That's why this project has a **separate total budget** (900 ms across all attempts) enforced with
`asyncio.timeout`. Two mechanisms, two jobs: httpx bounds each attempt's idleness, the budget bounds
the whole request.

A subtlety worth internalising: those two must **not** be set to the same value. If the httpx read
timeout and the asyncio ceiling are both 200 ms, they race, and which one fires is effectively random
— so the recorded reason for the failure alternates between runs and tests flake. Setting the outer
ceiling ~15 % higher makes httpx reliably win, which is what you want, because httpx knows *why* it
failed while `asyncio.timeout` only knows *that* it did.

**The general rule:** a proxy's total timeout must be shorter than its caller's. If your client gives
up at 1 s and you're starting a third attempt at 900 ms, that work is discarded and every backend paid
for it anyway.

---

## 3. Failure modes are not interchangeable

The single most useful thing this project teaches: "the region went down" describes at least four
different physical events, and they produce different exceptions, different latencies, and different
correct responses.

| What you do | What the client sees | How long it takes |
|---|---|---|
| `docker stop` | `ConnectError` — Docker's DNS resolver withdraws the record, so the *name* stops resolving | ~2 ms |
| `docker stop`, with a warm connection pool | `RemoteProtocolError` — no DNS lookup happens, you write into a socket whose peer is gone | ~2 ms |
| `docker kill` | `ReadError` / `RemoteProtocolError` — RST on pooled sockets | ~2 ms |
| `docker pause` | `ReadTimeout` — the kernel still ACKs the SYN from the listen backlog, so connect *succeeds*, then silence | **200 ms** |
| `docker network disconnect` | `ConnectTimeout` — the SYN goes unanswered | 100 ms |

**The punchline for this brief:** it asks for a 200 ms timeout *and* specifies `docker stop` as the
fault injection. Those two never meet. `docker stop` fails via DNS in ~2 ms — the read timeout is
never reached. If you only ever test with `docker stop`, your headline timeout is completely
unexercised and you'd never know.

Hence `make chaos-pause-us-east`. Same code path, same `except` clause, ~100× the latency, a different
exception class. Running both and comparing is the most instructive thing you can do with this
repository.

Two more things worth knowing:

- `httpx.ConnectTimeout` is **not** a subclass of `ConnectError`, despite the names. Catching only
  `ConnectError` silently misses network partitions.
- A **warm connection pool changes the exception**. The first request to a dead container fails at
  DNS; a request reusing an established keep-alive socket never does a lookup and gets a protocol
  error instead. This is why catching only `TimeoutException` misses the most common real case.

---

## 4. Passive vs active failure detection

**Passive** (what this project does): find out a region is down by trying it and failing. Zero extra
traffic, and it reflects the exact path real requests take. The cost is that every request during an
outage pays the failure latency.

**Active** (health polling): a background task probes each region every second. Requests never pay the
discovery cost. The costs are constant background traffic, a second definition of "healthy" that can
disagree with reality, and — subtly — the probe path is often not the request path, so a region can
pass health checks while failing real work.

This project uses passive detection only, and that's partly a deliberate constraint: an active poller
would mark `us-east-1` down *before any real request failed*, which means the `try`/`except` the brief
asks for would never fire. See [ADR-009](DECISIONS.md#adr-009).

---

## 5. Circuit breakers, and the arithmetic that justifies them

Without a breaker, a dead primary is a **permanent tax**. Every request tries `us-east-1`, waits for
it to fail, then moves on. With a `docker pause`-style failure at 200 ms, every single request during
the outage is 200 ms slower — for the entire duration.

A breaker remembers. After *k* consecutive failures it stops trying, and the tax disappears after the
first few requests instead of persisting for the whole outage.

```
CLOSED  --k consecutive failures-->  OPEN
OPEN    --cooldown elapsed-------->  HALF_OPEN
HALF_OPEN --probe succeeds------->   CLOSED
HALF_OPEN --probe fails---------->   OPEN (longer cooldown)
```

`HALF_OPEN` must admit **exactly one** probe. Otherwise every in-flight request stampedes a region
that is still recovering, and knocks it back down.

Opening the breaker should also **purge that region's connection pool**. Stale keep-alive sockets to a
container that restarted are precisely what produce `RemoteProtocolError` later.

One implementation trap that's easy to miss: if the breaker's "on open" hook is a synchronous callback
that returns a coroutine, nobody awaits it and the purge silently never happens. It has to go through
`asyncio.create_task`, with the task stored in a module-level set so garbage collection doesn't drop it
mid-flight.

---

## 6. Chaos engineering: pick the fault deliberately

Chaos engineering isn't "break things randomly". It's: state a hypothesis about how the system
behaves under a specific fault, inject exactly that fault, and check.

The fault you choose determines the code path you test:

| Primitive | Tests |
|---|---|
| `docker stop` | Name resolution failure, fast-fail path |
| `docker kill` | Abrupt connection reset, warm-pool path |
| `docker pause` | Read timeout path — the *only* one that exercises a timeout |
| `network disconnect` | Connect timeout, partition path |
| App-level 5xx injection | Response-level failure, not transport |
| Stopping the dependency (Redis) | Fail-open/fail-closed policy |

### Measuring it without lying to yourself

Two ways a resilience demo flatters itself:

**Coordinated omission.** If your load generator uses a fixed pool of workers that each wait for a
response before sending the next request, then when the system stalls, the generator stops sending.
The latency spike you were trying to measure never enters the dataset. The fix is an **open-loop**
pacer: issue requests on a fixed schedule regardless of whether earlier ones returned.

**Timing windows keyed on the wrong event.** "No request was served by us-east-1 after the kill" sounds
right, but a request correctly dispatched to a *healthy* region microseconds before the kill can land
afterwards. Evaluate on **send** time, not receive time, or your assertion fails for correct behaviour.

This harness also refuses to report a run where achieved throughput fell below 90 % of target — a
result gathered under a load the generator couldn't sustain isn't evidence of anything.

---

## 7. `X-Forwarded-For` is attacker input

Rate limiting by IP requires knowing the IP. Behind any proxy, `request.client.host` is the proxy's
address — inside Docker it's frequently the bridge gateway, identical for every caller, so all clients
share one bucket.

So you read `X-Forwarded-For`. But **the client sets that header**, so it's attacker input.

The rule: only trust XFF if the *direct peer* is a proxy you trust, then walk the chain right-to-left
and take the first entry that isn't a trusted proxy.

The trap is the fallback when every entry is trusted. If you fall back to `chain[0]` — the leftmost
value — you've handed control to the attacker:

```
X-Forwarded-For: 1.2.3.4   ->  bucket for 1.2.3.4   (fresh, full)
X-Forwarded-For: 1.2.3.5   ->  bucket for 1.2.3.5   (fresh, full)
```

Every request mints a new full bucket. The rate limiter does nothing while appearing to work
perfectly. Fall back to the **peer** instead, and write the test that fires 200 distinct XFF values and
asserts they all land in one bucket.

---

## 8. Break it yourself

The fastest way to understand any of the above is to make it fail.

1. **Remove the `min(tokens, capacity)` clamp** in the Lua script. Leave a bucket idle for a minute,
   then flood it. Watch the limit stop meaning anything.
2. **Store tokens as an integer** instead of `%.6f`. Send a request every 10 ms and watch the bucket
   never refill.
3. **Cut the bucket's TTL below `capacity / refill_rate`.** Pace requests just slower than the TTL and
   observe unlimited free capacity.
4. **Compare `stop` and `pause`.** Run `make chaos` and `make chaos-pause-us-east`, then diff the
   `error_class` and the transition p50. Same code, ~100× the latency.
5. **Set the asyncio ceiling equal to the httpx read timeout.** Run the pause scenario ten times and
   watch the recorded failure code flip between `read_timeout` and `attempt_deadline`.
6. **Replace the Lua script with `GET`/`SET` in Python.** Run `rate-limit-storm` and count how many
   requests get through above capacity.
7. **Add `restart: unless-stopped` to a region.** Watch the chaos timeline become incoherent as the
   container resurrects mid-measurement.
8. **Switch to `MRFP_MODE=resilient`** and compare p50 during the outage against literal mode. That
   difference is exactly what the circuit breaker buys — and the drop in `upstream_failover` log lines
   is exactly what it costs you in brief fidelity.
