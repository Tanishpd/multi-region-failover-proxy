#!/usr/bin/env bash
# Rotate the Redis credential under a running production fleet.
#
# This is the operational failure most likely to actually happen: a secret is
# rotated and something is not redeployed with it. ADR-029 built the machinery
# for it — the `auth` classifier, the ERROR level, the boot refusal, two alerts
# and a runbook section — and every part of that was asserted rather than
# exercised until this script existed.
#
# It drills the whole chain against `deploy/compose.prod.yml`:
#
#   1. baseline: traffic flows, rate_limit_backend_up == 1
#   2. rotate requirepass and kill existing connections
#   3. traffic MUST keep flowing (fail-open is the designed behaviour)
#      and the limiter must report reason="auth" at ERROR, backend_up == 0
#   4. a RESTARTED replica must REFUSE to boot on the stale credential —
#      the runbook's claim that a fresh deploy cannot reach the degraded state
#   5. restore the credential: the limiter reattaches with no restart, and the
#      crash-looping replica recovers on its own
#
# Measured on this stack, all five hold.
set -euo pipefail

cd "$(dirname "$0")/../.."

PY=.venv/bin/python
[ -x "$PY" ] || { echo "FAIL: no venv. Run: make venv" >&2; exit 2; }

OLD_PW="${MRFP_REDIS_PASSWORD:-pw}"
NEW_PW="rotated-$$"
ADMIN="${MRFP_ADMIN_TOKEN:-drill}"
KEYS="$(mktemp -t mrfp-keys-XXXXXX).json"
FAILURES=0

note() { printf '\n== %s ==\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

cleanup() {
  # Always put the credential back, or the stack is left unusable.
  docker exec mrfp-prod-redis-1 redis-cli -a "$NEW_PW" --no-auth-warning \
    CONFIG SET requirepass "$OLD_PW" >/dev/null 2>&1 || true
  rm -f "$KEYS"
}
trap cleanup EXIT INT TERM

API_KEY=$("$PY" - "$KEYS" <<'PY'
import hashlib, json, secrets, sys
key = "mrfp_sk_" + secrets.token_urlsafe(32)
json.dump({"drill": {"sha256": hashlib.sha256(key.encode()).hexdigest(),
                     "capacity": 1000, "refill_per_sec": 500}}, open(sys.argv[1], "w"))
print(key)
PY
)

# From a CLEAN stack, always. A previous run — or a hand-run experiment — can
# leave Redis on a rotated password, and compose will not recreate an unchanged
# container, so the proxy correctly refuses to boot and the drill fails during
# setup for a reason that has nothing to do with what it measures.
note "tearing down any previous production stack"
bash ops/scripts/prod.sh down >/dev/null 2>&1 || true

note "starting the production profile"
MRFP_REDIS_URL="redis://:${OLD_PW}@redis:6379/0" \
  MRFP_REDIS_PASSWORD="$OLD_PW" \
  MRFP_TRUSTED_PROXIES="10.0.0.0/8,127.0.0.1/32" \
  MRFP_ADMIN_TOKEN="$ADMIN" \
  MRFP_API_KEYS_FILE="$KEYS" \
  bash ops/scripts/prod.sh up >/dev/null
sleep 3

drive() {  # $1 = how many requests; prints "<ok> <failed>"
  docker exec -i -e K="$API_KEY" -e N="$1" mrfp-prod-proxy-1 python - <<'PY'
import json, os, urllib.request
ok = err = 0
for _ in range(int(os.environ["N"])):
    req = urllib.request.Request(
        "http://127.0.0.1:8000/v1/infer",
        data=json.dumps({"prompt": "drill", "max_tokens": 4}).encode(),
        headers={"content-type": "application/json",
                 "authorization": "Bearer " + os.environ["K"],
                 "x-forwarded-proto": "https", "x-forwarded-for": "10.9.9.9"})
    try:
        urllib.request.urlopen(req, timeout=30); ok += 1
    except Exception:
        err += 1
print(ok, err)
PY
}

gauge() {  # $1 = metric name
  docker exec -i -e M="$1" -e T="$ADMIN" mrfp-prod-proxy-1 python - <<'PY'
import os, urllib.request
req = urllib.request.Request("http://127.0.0.1:8000/metrics",
                             headers={"authorization": "Bearer " + os.environ["T"]})
for line in urllib.request.urlopen(req).read().decode().splitlines():
    if line.startswith(os.environ["M"] + " "):
        print(line.split()[1]); break
else:
    print("absent")
PY
}

note "1. baseline"
read -r OK ERR <<<"$(drive 6)"
echo "traffic: ${OK} ok, ${ERR} failed   backend_up=$(gauge rate_limit_backend_up)"
[ "$ERR" = "0" ] || fail "baseline traffic already failing"
[ "$(gauge rate_limit_backend_up)" = "1.0" ] || fail "limiter not on Redis at baseline"

note "2. rotating the credential out from under the fleet"
docker exec mrfp-prod-redis-1 redis-cli -a "$OLD_PW" --no-auth-warning \
  CONFIG SET requirepass "$NEW_PW" >/dev/null
docker exec mrfp-prod-redis-1 redis-cli -a "$NEW_PW" --no-auth-warning \
  CLIENT KILL TYPE normal >/dev/null
sleep 2

note "3. traffic must survive; the limiter must say auth"
read -r OK ERR <<<"$(drive 6)"
UP=$(gauge rate_limit_backend_up)
echo "traffic: ${OK} ok, ${ERR} failed   backend_up=${UP}"
[ "$ERR" = "0" ] || fail "$ERR requests failed — fail-open did not hold"
[ "$UP" = "0.0" ] || fail "rate_limit_backend_up=$UP; MRFPRateLimitDegraded would not fire"
# grep -c, never grep -q. `grep -q` exits on the first match, `docker logs`
# then takes SIGPIPE, and `set -o pipefail` reports the whole pipeline as
# failed EVEN THOUGH THE PATTERN MATCHED. This drill reported two false
# failures that way while the system underneath was behaving perfectly.
AUTH_LINES=$(docker logs mrfp-prod-proxy-1 2>&1 | grep -c '"event":"rate_limit_degraded".*"reason":"auth"' || true)
[ "${AUTH_LINES:-0}" -gt 0 ] \
  || fail "no rate_limit_degraded with reason=auth — MRFPRateLimitRefused would not fire"
echo "rate_limit_degraded reason=auth: ${AUTH_LINES} line(s)"

ERROR_LINES=$(docker logs mrfp-prod-proxy-1 2>&1 | grep '"reason":"auth"' | grep -c '"level":"ERROR"' || true)
[ "${ERROR_LINES:-0}" -gt 0 ] || fail "auth degradation logged below ERROR"

note "4. a restarted replica must REFUSE to boot on the stale credential"
docker restart mrfp-prod-proxy-2 >/dev/null
sleep 8
REFUSED=$(docker logs mrfp-prod-proxy-2 2>&1 | grep -c "RedisMisconfigured" || true)
[ "${REFUSED:-0}" -gt 0 ] \
  || fail "replica-2 started anyway — it would serve unlimited traffic reporting healthy"
echo "replica-2: $(docker inspect -f '{{.State.Status}}' mrfp-prod-proxy-2) (expected: restarting)"

note "5. restoring the credential"
docker exec mrfp-prod-redis-1 redis-cli -a "$NEW_PW" --no-auth-warning \
  CONFIG SET requirepass "$OLD_PW" >/dev/null
sleep 3
drive 4 >/dev/null
UP=$(gauge rate_limit_backend_up)
echo "backend_up=${UP} (reattached with no restart)"
[ "$UP" = "1.0" ] || fail "limiter did not reattach after the credential was restored"

for _ in $(seq 1 12); do
  [ "$(docker inspect -f '{{.State.Status}}' mrfp-prod-proxy-2)" = "running" ] && break
  sleep 10
done
[ "$(docker inspect -f '{{.State.Status}}' mrfp-prod-proxy-2)" = "running" ] \
  || fail "replica-2 never recovered after the credential was restored"

note "result"
if [ "$FAILURES" -eq 0 ]; then
  echo "PASS: fail-open held, auth was distinguished from an outage, a stale"
  echo "      deploy refused to start, and everything recovered unattended."
else
  echo "FAILED ${FAILURES} check(s)"
fi
bash ops/scripts/prod.sh down >/dev/null 2>&1 || true
exit "$FAILURES"
