#!/usr/bin/env bash
#
# Create/refresh the host virtualenv used by chaos/ and tests/unit.
#
# Nothing that imports the application runs on the host — that all lives in
# containers. This venv exists because the chaos harness needs the docker CLI
# (so it cannot run inside the stack it is breaking), and because `make
# test-unit` must work with the Docker daemon stopped.
#
# Built on python3.13 rather than the host default 3.14: the container base is
# 3.12, and 3.13 is the version where every pure-Python dependency here has a
# settled wheel. (ADR-015)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$ROOT/.venv"
REQ="$ROOT/requirements-dev.txt"

STAMP="$VENV/.requirements.sha"
CURRENT="$(shasum -a 256 "$REQ" | awk '{print $1}')"

if [[ -x "$VENV/bin/python" && -f "$STAMP" ]] && [[ "$(cat "$STAMP")" == "$CURRENT" ]]; then
  exit 0
fi

PYBIN=""
for candidate in /opt/homebrew/bin/python3.13 /usr/local/bin/python3.13 python3.13 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PYBIN="$(command -v "$candidate")"; break; fi
done
if [[ -z "$PYBIN" ]]; then
  printf 'venv: no python3 interpreter found. Install python3.13.\n' >&2
  exit 2
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  printf 'venv: creating %s with %s\n' "${VENV#"$ROOT"/}" "$("$PYBIN" --version 2>&1)"
  "$PYBIN" -m venv "$VENV"
fi

printf 'venv: installing dev requirements\n'
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$REQ"

printf '%s' "$CURRENT" > "$STAMP"
printf 'venv: ready\n'
