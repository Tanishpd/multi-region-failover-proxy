#!/usr/bin/env bash
#
# Placeholder for a target whose phase has not been built yet.
#
# The Makefile carries the complete command surface from day one so it matches
# CLAUDE.md section 4, but the project is built in phases (CLAUDE.md section 9).
# Rather than failing with "No such file or directory", an unbuilt target says
# so and points at the build order. Exits non-zero so nothing mistakes it for
# a successful run.
set -euo pipefail

PHASE="${1:?phase number required}"
TARGET="${2:?target name required}"

if [[ -t 2 ]]; then Y=$'\033[33m'; D=$'\033[2m'; O=$'\033[0m'; else Y=''; D=''; O=''; fi

printf '\n  %s%s is not built yet%s — it lands in Phase %s.\n' "$Y" "$TARGET" "$O" "$PHASE" >&2
printf '  %sBuild order: CLAUDE.md section 9. Available now: make doctor, make test-unit.%s\n\n' \
  "$D" "$O" >&2
exit 2
