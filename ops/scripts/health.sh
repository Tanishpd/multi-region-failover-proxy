#!/usr/bin/env bash
#
# One line per region: container state, health, and what the region says about
# itself. Reads from the host ports, so it works whether or not the proxy is up.
set -euo pipefail

# shellcheck source=ops/scripts/compose.sh
source "$(dirname "${BASH_SOURCE[0]}")/compose.sh"
require_daemon

port_for() {
  case "$1" in
    us-east-1-endpoint)      echo 9001 ;;
    eu-west-1-endpoint)      echo 9002 ;;
    ap-southeast-1-endpoint) echo 9003 ;;
  esac
}

printf '%-26s %-10s %-10s %s\n' "CONTAINER" "STATE" "HEALTH" "REGION SAYS"
printf '%-26s %-10s %-10s %s\n' "---------" "-----" "------" "-----------"

for name in "${REGIONS[@]}"; do
  state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo 'absent')"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' \
            "$name" 2>/dev/null || echo '-')"

  says='-'
  if [[ "$state" == "running" ]]; then
    port="$(port_for "$name")"
    body="$(curl -s --max-time 2 "http://127.0.0.1:${port}/" 2>/dev/null || true)"
    if [[ -n "$body" ]]; then
      says="$(printf '%s' "$body" | jq -r '"\(.region) · \(.base_latency_ms)ms · chaos=\(.chaos.mode // "none")"' 2>/dev/null || echo 'unparseable')"
    else
      says='no response'
    fi
  fi

  colour="$C_OK"
  [[ "$state" == "running" ]] || colour="$C_BAD"
  printf '%-26s %s%-10s%s %-10s %s\n' "$name" "$colour" "$state" "$C_OFF" "$health" "$says"
done

rstate="$(docker inspect -f '{{.State.Status}}' redis 2>/dev/null || echo 'absent')"
rhealth="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' \
           redis 2>/dev/null || echo '-')"
rcolour="$C_OK"; [[ "$rstate" == "running" ]] || rcolour="$C_BAD"
printf '%-26s %s%-10s%s %-10s %s\n' "redis" "$rcolour" "$rstate" "$C_OFF" "$rhealth" \
  "host port 6380 (6379 is the native one)"
