#!/usr/bin/env bash
#
# The production profile. Separate from ops/scripts/compose.sh on purpose:
# that file hard-pins -f docker-compose.yml, which is what structurally
# prevents `make chaos` from ever loading the prod topology.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FILE="$ROOT/deploy/compose.prod.yml"
export COMPOSE_PROJECT_NAME="mrfp-prod"

dcp() {
  docker compose --project-directory "$ROOT/deploy" -f "$FILE" "$@"
}

case "${1:-config}" in
  config) dcp config ;;
  up)     dcp up -d --wait --wait-timeout 120 ;;
  down)   dcp down --remove-orphans ;;
  *)      printf 'prod: unknown action %q (config|up|down)\n' "${1}" >&2; exit 2 ;;
esac
