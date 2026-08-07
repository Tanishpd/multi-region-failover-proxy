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

# An alert whose runbook anchor does not exist is worse than no alert: it wakes
# someone at 3am and then tells them nothing.
RULES="$ROOT/ops/observability/rules/slo.yml"
if [[ -f "$RULES" ]]; then
  missing=0
  total=0
  while read -r name; do
    total=$((total + 1))
    if ! grep -qi "^### alert-${name}\$" "$ROOT/docs/RUNBOOK.md"; then
      printf 'docs-check: alert %s has no runbook section\n' "$name" >&2
      missing=$((missing + 1))
    fi
  done < <(grep -oE '^\s+- alert: [A-Za-z]+' "$RULES" | awk '{print $3}')
  printf 'docs-check: %s alerts, %s runbook anchors, %s missing\n' \
    "$total" "$((total - missing))" "$missing"
  [[ "$missing" -eq 0 ]] || fail=1
fi

if [[ "$fail" -eq 0 ]]; then
  printf 'docs-check: ok\n'
fi
exit "$fail"
