#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=ops/scripts/compose.sh
source "$(dirname "${BASH_SOURCE[0]}")/compose.sh"
require_daemon

dc down --remove-orphans
printf 'down: stack removed\n'
