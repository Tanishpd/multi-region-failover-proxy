#!/usr/bin/env bash
#
# Follow proxy logs. Pretty-prints through jq when it is available and stdout is
# a terminal; raw JSON otherwise, so piping into jq downstream still works.
set -euo pipefail

# shellcheck source=ops/scripts/compose.sh
source "$(dirname "${BASH_SOURCE[0]}")/compose.sh"
require_daemon

SERVICE="${1:-mrfp-proxy}"

if command -v jq >/dev/null 2>&1 && [[ -t 1 ]]; then
  dc logs -f --no-log-prefix "$SERVICE" | jq -R 'fromjson? // .'
else
  dc logs -f --no-log-prefix "$SERVICE"
fi
