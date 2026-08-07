#!/usr/bin/env bash
#
# Start the stack and wait for health.
#
# Usage: up.sh [regions|all]
#   regions  regions + redis, without the proxy (Phase 1)
#   all      everything (default once the proxy exists)
set -euo pipefail

# shellcheck source=ops/scripts/compose.sh
source "$(dirname "${BASH_SOURCE[0]}")/compose.sh"
require_daemon

SCOPE="${1:-all}"

# Bring the images up without building. `make build` is a separate target
# because a cold build runs 3-6 minutes and a health-wait deadline started here
# would expire during it and report a false failure.
if [[ "$SCOPE" == "regions" ]]; then
  TARGETS=("${REGIONS[@]}" redis redis-sentinel-seed)
else
  TARGETS=()
fi

printf 'up: starting %s\n' "${SCOPE}"

# --wait blocks until every service with a healthcheck is healthy. The seed
# container runs to completion and exits 0, so it is excluded from the wait.
if [[ ${#TARGETS[@]} -gt 0 ]]; then
  dc up -d --wait --wait-timeout 90 "${REGIONS[@]}" redis
  dc up -d redis-sentinel-seed
else
  dc up -d --wait --wait-timeout 90
fi

printf '\n'
bash "$(dirname "${BASH_SOURCE[0]}")/health.sh"
