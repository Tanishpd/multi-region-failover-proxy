#!/usr/bin/env bash
#
# Run a chaos scenario. Exits with the harness's exit code, so `make chaos`
# genuinely gates.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=ops/scripts/compose.sh
source "$(dirname "${BASH_SOURCE[0]}")/compose.sh"
require_daemon

SCENARIO="${1:-stop-us-east}"

bash "$ROOT/ops/scripts/venv.sh"

# The flood must measure FAILOVER, not the rate limiter.
#
# All host traffic resolves to one bucket (the Docker bridge gateway), so a
# 50 rps flood against a 20-token bucket would 429 most of itself and the run
# would measure requirement 2 while claiming to measure requirement 4. The
# harness sends X-Chaos-Bypass, but the proxy only honours it when started with
# MRFP_CHAOS_MODE=1 — so ensure that, rather than failing with a confusing
# wall of 429s.
current="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' mrfp-proxy 2>/dev/null \
           | grep '^MRFP_CHAOS_MODE=' || true)"

# A one-off token for this run only. Never a committed default: the bypass
# skips rate limiting completely, and an earlier version left the proxy armed
# with a literal from the public repo for the life of the container.
CHAOS_TOKEN="chaos-$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"
export MRFP_CHAOS_TOKEN="$CHAOS_TOKEN"

restore() {
  printf '\nchaos: disarming the rate-limit bypass\n'
  MRFP_CHAOS_MODE=0 MRFP_CHAOS_TOKEN= dc up -d --force-recreate --wait --wait-timeout 60 \
    mrfp-proxy >/dev/null 2>&1 || true
}

if [[ "$current" != "MRFP_CHAOS_MODE=1" ]]; then
  printf 'chaos: arming the rate-limit bypass for this run only\n'
  MRFP_CHAOS_MODE=1 dc up -d --force-recreate --wait --wait-timeout 60 mrfp-proxy >/dev/null
  # Disarm on ANY exit, including a failed run or Ctrl-C. The proxy must not be
  # left in a state where one header defeats the limiter.
  trap restore EXIT INT TERM
  # Let the fresh proxy warm its connection pools, so the baseline window is not
  # measuring cold starts.
  sleep 2
fi

cd "$ROOT"
# Not exec: the EXIT trap has to run after the harness finishes.
"$ROOT/.venv/bin/python" -m chaos.run --scenario "$SCENARIO" "${@:2}"
