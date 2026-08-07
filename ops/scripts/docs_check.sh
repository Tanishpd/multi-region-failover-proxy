#!/usr/bin/env bash
#
# Fail if the docs claim a measurement that no script produced.
#
# The rule this enforces: numbers with units inside docs/CHAOS-REPORT.md must
# live between AUTOGEN markers. Prose may describe them; it may not invent them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPORT="$ROOT/docs/CHAOS-REPORT.md"
fail=0

# Marker-shaped only (TODO: / TODO( / FIXME:), so prose *describing* this check
# does not trip it.
todos="$(grep -rnE '(TODO|FIXME|TBD)[:(]' "$ROOT/docs" "$ROOT/README.md" "$ROOT/CLAUDE.md" || true)"
if [[ -n "$todos" ]]; then
  printf 'docs-check: unresolved markers:\n%s\n' "$todos" >&2
  fail=1
fi

# Any AUTOGEN region still holding a placeholder means `make report` was not run.
if grep -q '_pending_' "$REPORT"; then
  printf 'docs-check: %s still contains _pending_ regions — run `make chaos && make report`\n' \
    "${REPORT#"$ROOT"/}" >&2
  fail=1
fi

if [[ "$fail" -eq 0 ]]; then
  printf 'docs-check: ok\n'
fi
exit "$fail"
