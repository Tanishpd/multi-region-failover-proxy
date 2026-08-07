#!/usr/bin/env bash
# Measure what the regions actually cost, and derive the timeouts from that.
#
# `make calibrate`        measure and report
# `make calibrate-check`  the same, but exit non-zero if the PROD profile's
#                         timeouts are below what was measured
#
# Carries its own `set -euo pipefail` because this machine's make is GNU 3.81,
# where `.SHELLFLAGS` is silently ignored and a failing pipeline exits 0.
set -euo pipefail

cd "$(dirname "$0")/../.."

PY=.venv/bin/python
[ -x "$PY" ] || { echo "FAIL: no venv. Run: make venv" >&2; exit 2; }

for port in 9001 9002 9003; do
  if ! curl -fsS -m 2 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
    echo "FAIL: no region answering on 127.0.0.1:${port}. Run: make up" >&2
    exit 2
  fi
done

if [ "${1:-}" = "check" ]; then
  # The values the PROD profile would boot with. Compared against the
  # measurement so a timeout can never silently drift below what the backend
  # needs. Read from profiles.py rather than restated here, or this gate would
  # pass against numbers nothing actually uses.
  eval "$($PY - <<'PY'
import sys
sys.path.insert(0, "proxy")
from app.profiles import PROD, preset
prod = preset(PROD)
print(f'READ={prod.get("read_timeout_ms", 200)}')
print(f'BUDGET={prod.get("total_budget_ms", 900)}')
print(f'CHUNK={prod.get("stream_inter_chunk_timeout_s", 60.0)}')
print(f'ATTEMPTS={prod.get("region_attempt_limit", 3)}')
PY
)"
  exec "$PY" -m chaos.calibrate \
    --check \
    --attempts "$ATTEMPTS" \
    --configured-read-ms "$READ" \
    --configured-budget-ms "$BUDGET" \
    --configured-inter-chunk-s "$CHUNK" \
    "${@:2}"
fi

exec "$PY" -m chaos.calibrate --write-doc "$@"
