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

# MRFP_CHAOS_MODE=1 lets the flood bypass rate limiting, so the run measures
# failover rather than the token bucket. The proxy only honours the bypass
# header when it is started with this set.
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m chaos.run --scenario "$SCENARIO" "${@:2}"
