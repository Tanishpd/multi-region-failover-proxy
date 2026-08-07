#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=ops/scripts/compose.sh
source "$(dirname "${BASH_SOURCE[0]}")/compose.sh"
require_daemon

dc ps --format 'table {{.Name}}\t{{.Service}}\t{{.Status}}\t{{.Ports}}'
