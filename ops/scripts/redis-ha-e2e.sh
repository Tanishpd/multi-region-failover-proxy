#!/usr/bin/env bash
# Redis HA in anger: kill the primary under LIVE PROXY TRAFFIC.
#
# redis-ha-drill.sh proves the redis client survives a promotion. This proves
# the thing that actually matters — that a caller of /v1/infer never sees it.
# The limiter should fail open to its local bucket for the few seconds Sentinel
# needs, then reattach, with no client-visible error at any point.
#
# Requires: `make up`, `make redis-ha-up`, and a proxy pointed at the sentinels:
#   MRFP_REDIS_SENTINELS="sentinel-1:26379,sentinel-2:26379,sentinel-3:26379" \
#     docker compose up -d mrfp-proxy --wait
set -euo pipefail

cd "$(dirname "$0")/../.."

PY=.venv/bin/python
[ -x "$PY" ] || { echo "FAIL: no venv. Run: make venv" >&2; exit 2; }

KILL_AT_S="${KILL_AT_S:-8}"
export PROBE_DURATION_S="${PROBE_DURATION_S:-25}"
# Deliberately BELOW the configured limit (capacity 20, refill 20/s). A drill
# that 429s on its own load cannot tell a rate-limit rejection caused by the
# failover from one it caused itself.
export PROBE_RPS="${PROBE_RPS:-10}"

docker ps --format '{{.Names}}' | grep -q '^mrfp-sentinel-1$' \
  || { echo "FAIL: HA topology not running. Run: make redis-ha-up" >&2; exit 2; }

TOPOLOGY=$(curl -fsS -m 5 localhost:8000/readyz | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["rate_limit"]["boot"])')
[ "$TOPOLOGY" = "ok" ] || { echo "FAIL: proxy has no working Redis (boot=$TOPOLOGY)" >&2; exit 2; }

echo "== waiting for a healthy topology =="
for attempt in $(seq 1 60); do
  REPLICAS=$(docker exec mrfp-sentinel-1 redis-cli -p 26379 \
    sentinel replicas mrfp-master 2>/dev/null | tr -d '\r' || true)
  LINK=$(echo "$REPLICAS" | grep -A1 '^master-link-status$' | tail -1)
  [ "$LINK" = "ok" ] && { echo "healthy after ${attempt}s"; break; }
  [ "$attempt" = 60 ] && { echo "FAIL: no in-sync replica; nothing to promote" >&2; exit 2; }
  sleep 1
done

ADDR=$(docker exec mrfp-sentinel-1 redis-cli -p 26379 \
  sentinel get-master-addr-by-name mrfp-master | tr -d '\r' | tr '\n' ' ')
MASTER=$(echo "$ADDR" | awk '{print $1}')

TARGET=""
for candidate in mrfp-redis-primary mrfp-redis-replica; do
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$candidate" 2>/dev/null || true)
  service="${candidate#mrfp-}"
  { [ "$ip" = "$MASTER" ] || [ "$service" = "$MASTER" ]; } && TARGET="$candidate"
done
[ -n "$TARGET" ] || { echo "FAIL: no container owns $MASTER" >&2; exit 2; }
echo "primary is $TARGET ($MASTER)"

(
  sleep "$KILL_AT_S"
  echo ">> docker stop $TARGET" >&2
  docker stop "$TARGET" >/dev/null
) &
KILLER=$!

echo "== ${PROBE_RPS} rps through the proxy for ${PROBE_DURATION_S}s =="
RESULT=$("$PY" ops/drills/proxy_probe.py)
wait "$KILLER"

echo "$RESULT" | "$PY" -m json.tool

# Restore before asserting, so a failure still leaves a usable topology.
docker start "$TARGET" >/dev/null 2>&1 || true

DEGRADED=$(docker compose logs --no-log-prefix mrfp-proxy 2>/dev/null \
  | grep -c '"event":"rate_limit_degraded"' || true)
BACKEND_UP=$(curl -fsS -m 5 localhost:8000/metrics | awk '/^rate_limit_backend_up /{print $2}')
echo "rate_limit_degraded lines: ${DEGRADED}   backend_up now: ${BACKEND_UP}"

RESULT="$RESULT" DEGRADED="$DEGRADED" BACKEND_UP="$BACKEND_UP" "$PY" - <<'PY'
import json, os, sys
r = json.loads(os.environ["RESULT"])
degraded = int(os.environ["DEGRADED"])
backend_up = float(os.environ["BACKEND_UP"] or 0)
fails = []

# THE assertion. Losing the Redis primary must be invisible to the caller.
if r["client_errors"]:
    fails.append(f"{r['client_errors']} client-side failures")
if r["server_errors"]:
    fails.append(f"{r['server_errors']} 5xx returned to clients")

# Below the configured limit, so any 429 means the fallback enforced far more
# harshly than the shared bucket did — a real regression, not noise.
if r["statuses"].get("429"):
    fails.append(f"{r['statuses']['429']} rate-limit rejections at {os.environ.get('PROBE_RPS','?')} rps")

# The run must actually have exercised the degradation, or it proves nothing.
if degraded == 0:
    fails.append("no rate_limit_degraded lines: the outage never reached the limiter")

# And it must have come back.
if backend_up != 1.0:
    fails.append(f"limiter did not reattach to Redis (rate_limit_backend_up={backend_up})")

for f in fails:
    print(f"FAIL: {f}")
sys.exit(1) if fails else print(
    f"PASS: {r['total']} requests, 0 errors, p50 {r['p50_ms']}ms / max {r['max_ms']}ms, "
    f"{degraded} degraded decisions, limiter reattached"
)
PY
