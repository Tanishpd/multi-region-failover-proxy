#!/usr/bin/env bash
#
# ApacheBench throughput cross-check, independent of the async harness.
#
# `ab` MUST get -p and -T: /v1/infer is POST-only, so a bare `ab` would measure
# the 405 path and report a flattering number for work never done. -k enables
# keep-alive, which ab does not use by default.
#
# ab cannot read response headers, so it says nothing about which region served
# what — that is what chaos/ is for. This exists only as a second opinion on
# raw throughput.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$(dirname "${BASH_SOURCE[0]}")/compose.sh"
require_daemon

N="${1:-2000}"
C="${2:-50}"

printf 'bench: %s requests, concurrency %s (rate limiting applies — expect 429s)\n\n' "$N" "$C"
ab -k -n "$N" -c "$C" -p "$ROOT/ops/bench/payload.json" -T application/json \
   http://127.0.0.1:8000/v1/infer
