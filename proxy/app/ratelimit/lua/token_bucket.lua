-- token_bucket.lua v1 — atomic refill / decide / debit / expire in ONE round trip.
--
-- Read-modify-write on a shared counter from the client is a lost update: two
-- workers GET 5, both compute 4, both SET 4 — two requests served, one token
-- spent. Redis runs a script single-threaded start to finish with nothing
-- interleaved, so doing the whole sequence here is atomic by construction. No
-- WATCH loop, no retries under exactly the load where you can least afford them.
--
-- KEYS[1..N]  bucket HASH keys, hash-tagged so all of one client's layers share
--             a slot: rl:v1:{ip:203.0.113.9}:burst
--             fields: t = tokens (string, %.6f), ts = last refill (unix ms, %.0f)
-- ARGV[1]     mode: "consume" | "peek"
-- ARGV[2]     cost
-- ARGV[3]     now_override_ms (0 = use redis TIME; non-zero only for tests)
-- ARGV[4 + 3*(i-1)] per layer: capacity_i, refill_per_sec_i, ttl_pad_ms_i
--
-- RETURNS a flat INTEGER array. Lua->RESP conversion TRUNCATES floats, so no
-- float ever leaves this script; tokens come back multiplied by 1000.
--   [1] allowed  [2] limiting_layer  [3] retry_after_ms  [4] reset_after_ms
--   [5] now_ms   then 3 per layer: tokens_milli_i, remaining_i, capacity_i

if redis.replicate_commands then redis.replicate_commands() end

local mode         = ARGV[1]
local cost         = tonumber(ARGV[2])
local now_override = tonumber(ARGV[3])

local now_ms
if now_override and now_override > 0 then
  now_ms = now_override
else
  -- One clock, Redis's own. Taking the time from the client instead would mean
  -- N workers with N slightly different opinions about now, and a bucket that
  -- refills or starves depending on which worker happened to serve you.
  local t = redis.call('TIME')
  now_ms = (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end

local n = #KEYS
local caps, rates, ttls, toks = {}, {}, {}, {}
local allowed, limiting, retry_after_ms = 1, 0, 0

-- PASS 1: read and lazily refill every layer, then decide. Write NOTHING yet.
for i = 1, n do
  local b    = 4 + (i - 1) * 3
  local cap  = tonumber(ARGV[b])
  local rate = tonumber(ARGV[b + 1])
  local pad  = tonumber(ARGV[b + 2])
  caps[i], rates[i] = cap, rate

  local st = redis.call('HMGET', KEYS[i], 't', 'ts')
  local tk, ts
  if st[1] == false or st[2] == false then
    tk, ts = cap, now_ms                       -- first sight: a full bucket
  else
    tk, ts = tonumber(st[1]), tonumber(st[2])
    if tk == nil or ts == nil then tk, ts = cap, now_ms end
  end

  -- Lazy refill: convert elapsed time into tokens on read, so idle buckets cost
  -- nothing and there is no scheduler to fail.
  local elapsed = now_ms - ts
  if elapsed < 0 then elapsed = 0 end          -- clock stepped back: never refund
  if rate > 0 and elapsed > 0 then
    tk = tk + (elapsed * rate / 1000.0)
  end
  if tk > cap then tk = cap end                -- CLAMP: an idle hour is not 72000 tokens
  if tk < 0  then tk = 0  end
  toks[i] = tk

  -- TTL must be >= the exact full-refill time. Expire sooner and the next
  -- request finds no key, assumes a fresh full bucket, and anyone pacing just
  -- slower than the TTL gets unlimited capacity for free.
  local full_ms
  if rate > 0 then full_ms = math.ceil((cap / rate) * 1000) else full_ms = 86400000 end
  ttls[i] = full_ms + pad

  if tk < cost then
    if allowed == 1 then limiting = i end
    allowed = 0
    local ra
    if rate > 0 then ra = math.ceil(((cost - tk) / rate) * 1000) else ra = ttls[i] end
    if ra > retry_after_ms then retry_after_ms = ra end
  end
end

-- PASS 2: commit. All-or-nothing across layers: debiting layer 1 before
-- discovering layer 2 denies would charge a client for a request it never got,
-- so a 429'd caller would still drain its budget.
local reset_after_ms = 0
for i = 1, n do
  local tk = toks[i]
  if allowed == 1 and mode == 'consume' and cost > 0 then
    tk = tk - cost
    toks[i] = tk
  end
  if mode == 'consume' then
    if allowed == 1 then
      redis.call('HSET', KEYS[i],
                 -- PERSIST THE FRACTION. Flooring starves the bucket forever:
                 -- at 20 tok/s a 10 ms gap accrues 0.2 tokens, which floors to
                 -- 0, so a client polling every 10 ms never refills at all.
                 't',  string.format('%.6f', tk),
                 -- %.0f, not tostring(): Lua 5.1 formats numbers with %.14g,
                 -- which mangles anything past 14 significant digits — and this
                 -- value gains a fraction the moment it comes from TIME
                 -- arithmetic.
                 'ts', string.format('%.0f', now_ms))
    end
    -- Refresh the TTL even on DENY. Otherwise a continuously rate-limited
    -- client lets its key expire and returns with a full bucket — being
    -- blocked would reward it.
    redis.call('PEXPIRE', KEYS[i], ttls[i])
  end
  local r = 0
  if rates[i] > 0 then r = math.ceil(((caps[i] - tk) / rates[i]) * 1000) end
  if r > reset_after_ms then reset_after_ms = r end
end

local out = { allowed, limiting, retry_after_ms, reset_after_ms, now_ms }
for i = 1, n do
  out[#out + 1] = math.floor(toks[i] * 1000)
  out[#out + 1] = math.floor(toks[i])
  out[#out + 1] = math.floor(caps[i])
end
return out
