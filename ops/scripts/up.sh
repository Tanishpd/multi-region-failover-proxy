#!/usr/bin/env bash
#
# Start the stack and wait for health.
#
# Usage: up.sh [regions|all]
#   regions  regions + redis, without the proxy
#   all      everything (default)
set -euo pipefail

# shellcheck source=ops/scripts/compose.sh
source "$(dirname "${BASH_SOURCE[0]}")/compose.sh"
require_daemon

SCOPE="${1:-all}"

# The wait list holds only LONG-RUNNING services. redis-guard-seed writes the
# test sentinel key and exits 0, and `--wait` reads any exit — including a clean
# one — as a failed startup.
case "$SCOPE" in
  regions) WAIT_FOR=(redis "${REGIONS[@]}") ;;
  all)     WAIT_FOR=(redis "${REGIONS[@]}" mrfp-proxy) ;;
  *)       printf 'up: unknown scope %q (expected: regions|all)\n' "$SCOPE" >&2; exit 2 ;;
esac

printf 'up: starting %s\n' "$SCOPE"
dc up -d --wait --wait-timeout 90 "${WAIT_FOR[@]}"

# Seed after Redis is healthy; run to completion rather than waiting on it.
dc up -d --no-deps redis-guard-seed >/dev/null

printf '\n'
bash "$(dirname "${BASH_SOURCE[0]}")/health.sh"
