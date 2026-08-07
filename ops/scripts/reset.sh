#!/usr/bin/env bash
#
# Flush the application's rate-limit keys. Scoped to db 0 and the rl:v1:*
# prefix; there is no FLUSHALL anywhere in this repository, because a real Redis
# with real data is listening one port away on 6379.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/compose.sh"
require_daemon

deleted="$(docker exec redis sh -c 'redis-cli -n 0 --scan --pattern "rl:v1:*" | xargs -r redis-cli -n 0 DEL' 2>/dev/null || echo 0)"
printf 'reset: cleared %s rate-limit key(s) from db 0\n' "${deleted:-0}"
