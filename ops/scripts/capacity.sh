#!/usr/bin/env bash
#
# Find the knee: the rps at which p99 degrades or the admission gate starts
# shedding. Writes the result into an AUTOGEN block in docs/SLO.md, so the
# published number is measured rather than asserted (CLAUDE.md section 10).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$(dirname "${BASH_SOURCE[0]}")/compose.sh"
require_daemon

bash "$ROOT/ops/scripts/venv.sh"

# Same arm-and-disarm dance as chaos.sh: a capacity run must measure the proxy,
# not the rate limiter, and the bypass must not outlive the measurement.
CHAOS_TOKEN="cap-$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"
export MRFP_CHAOS_TOKEN="$CHAOS_TOKEN"

restore() {
  printf '\ncapacity: disarming the rate-limit bypass\n'
  MRFP_CHAOS_MODE=0 MRFP_CHAOS_TOKEN= dc up -d --force-recreate --wait --wait-timeout 60 \
    mrfp-proxy >/dev/null 2>&1 || true
}
trap restore EXIT INT TERM

MRFP_CHAOS_MODE=1 dc up -d --force-recreate --wait --wait-timeout 60 mrfp-proxy >/dev/null
sleep 2

cd "$ROOT"
"$ROOT/.venv/bin/python" -m chaos.capacity "$@"
