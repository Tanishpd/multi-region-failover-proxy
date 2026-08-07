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

if [[ "$current" != "MRFP_CHAOS_MODE=1" ]]; then
  printf 'chaos: enabling the rate-limit bypass on mrfp-proxy (was %s)\n' "${current:-unset}"
  MRFP_CHAOS_MODE=1 dc up -d --force-recreate --wait --wait-timeout 60 mrfp-proxy >/dev/null
  # Give the fresh proxy a moment to warm its connection pools, so the first
  # seconds of the baseline window are not measuring cold starts.
  sleep 2
fi

cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m chaos.run --scenario "$SCENARIO" "${@:2}"
