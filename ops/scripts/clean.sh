#!/usr/bin/env bash
#
# Remove generated local state. Never touches containers, images, or Redis.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

rm -rf .venv .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# Chaos output: keep the directory, drop its contents.
if [[ -d artifacts ]]; then
  find artifacts -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -exec rm -rf {} + 2>/dev/null || true
fi

printf 'clean: removed .venv, caches, and artifacts/*\n'
