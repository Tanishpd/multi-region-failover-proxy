#!/usr/bin/env bash
#
# Shared helpers for every script that talks to Compose. Source, don't execute.
#
# Nothing here uses `cd` into a relative path or an unquoted expansion: the
# project directory contains spaces and an "&".

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export COMPOSE_PROJECT_NAME="mrfp"

REGIONS=(us-east-1-endpoint eu-west-1-endpoint ap-southeast-1-endpoint)

if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_BAD=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_OK=''; C_BAD=''; C_DIM=''; C_OFF=''
fi

dc() {
  docker compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" "$@"
}

require_daemon() {
  if ! docker info >/dev/null 2>&1; then
    printf '\n  %sDocker Desktop is not running%s  ->  open -a Docker\n' "$C_BAD" "$C_OFF" >&2
    printf '  %sThen wait ~30s and re-run. `make test-unit` works without it.%s\n\n' \
      "$C_DIM" "$C_OFF" >&2
    exit 2
  fi
}
