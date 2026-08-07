#!/usr/bin/env bash
#
# Build the images. Separate from `up` because a cold build runs 3-6 minutes.
set -euo pipefail

# shellcheck source=ops/scripts/compose.sh
source "$(dirname "${BASH_SOURCE[0]}")/compose.sh"
require_daemon

printf 'build: this takes 3-6 minutes the first time (two images, cold cache)\n\n'
dc build "$@"
printf '\nbuild: done — `make up` next\n'
