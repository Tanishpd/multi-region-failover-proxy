#!/usr/bin/env bash
#
# Preflight. Run this when anything is confusing.
#
# Every check prints PASS / WARN / FAIL with an actionable remedy. Exits 2 if
# anything FAILed, 0 otherwise — WARNs never fail the run, because two of them
# (native Redis on 6379, GNU Make 3.81) are permanent facts of this machine
# rather than problems to solve.
set -euo pipefail

# Resolve the repo root from this script's own location, never the CWD — the
# path contains spaces and an "&". (ADR-002)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

FAILED=0
WARNED=0

if [[ -t 1 ]]; then
  C_PASS=$'\033[32m'; C_WARN=$'\033[33m'; C_FAIL=$'\033[31m'
  C_DIM=$'\033[2m';  C_OFF=$'\033[0m'
else
  C_PASS=''; C_WARN=''; C_FAIL=''; C_DIM=''; C_OFF=''
fi

pass() { printf '  %sPASS%s  %-34s %s\n' "$C_PASS" "$C_OFF" "$1" "${2-}"; }
warn() { printf '  %sWARN%s  %-34s %s\n' "$C_WARN" "$C_OFF" "$1" "${2-}"; WARNED=$((WARNED + 1)); }
fail() { printf '  %sFAIL%s  %-34s %s\n' "$C_FAIL" "$C_OFF" "$1" "${2-}"; FAILED=$((FAILED + 1)); }
note() { printf '        %s%s%s\n' "$C_DIM" "$1" "$C_OFF"; }

# Is a TCP port listening on localhost? 0 = in use, 1 = free.
port_in_use() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

port_owner() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -Fc 2>/dev/null | grep '^c' | head -1 | cut -c2- || true
}

printf '\n%sMRFP preflight%s  %s\n\n' "$C_DIM" "$C_OFF" "$ROOT"

# --- toolchain --------------------------------------------------------------
printf '%stoolchain%s\n' "$C_DIM" "$C_OFF"

MAKE_VER="$(make --version 2>/dev/null | head -1 | awk '{print $3}' || echo '?')"
MAKE_MAJOR="${MAKE_VER%%.*}"
MAKE_MINOR="$(printf '%s' "$MAKE_VER" | cut -d. -f2)"
if [[ "$MAKE_MAJOR" == "3" && "${MAKE_MINOR:-0}" -lt 82 ]]; then
  warn "GNU Make $MAKE_VER" "predates .SHELLFLAGS (3.82)"
  note "Recipes must stay one command each, delegating pipelines to ops/scripts/*.sh."
  note "Otherwise a failing pipeline exits 0 and 'make chaos' reports a false PASS."
else
  pass "GNU Make $MAKE_VER" ""
fi

if command -v docker >/dev/null 2>&1; then
  pass "docker CLI" "$(docker --version 2>/dev/null | cut -d, -f1)"
else
  fail "docker CLI" "not found"
  note "Install Docker Desktop: https://docs.docker.com/desktop/install/mac-install/"
fi

if docker compose version >/dev/null 2>&1; then
  pass "docker compose" "$(docker compose version --short 2>/dev/null)"
else
  fail "docker compose" "the 'docker compose' subcommand is unavailable"
fi

for tool in git jq; do
  if command -v "$tool" >/dev/null 2>&1; then
    pass "$tool" ""
  else
    warn "$tool" "not found — some runbook recipes need it"
  fi
done

PYBIN=""
for candidate in /opt/homebrew/bin/python3.13 /usr/local/bin/python3.13 python3.13 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PYBIN="$(command -v "$candidate")"; break; fi
done
if [[ -n "$PYBIN" ]]; then
  pass "python (host venv)" "$("$PYBIN" --version 2>&1) at $PYBIN"
else
  fail "python (host venv)" "no python3 found"
fi

# --- docker daemon ----------------------------------------------------------
printf '\n%sdocker daemon%s\n' "$C_DIM" "$C_OFF"

if docker info >/dev/null 2>&1; then
  pass "daemon reachable" "$(docker info --format '{{.ServerVersion}} on {{.OperatingSystem}}' 2>/dev/null)"

  ARCH="$(docker info --format '{{.Architecture}}' 2>/dev/null || echo '?')"
  case "$ARCH" in
    aarch64|arm64) pass "architecture" "$ARCH (native)" ;;
    *)             warn "architecture" "$ARCH — expected arm64 on this host" ;;
  esac
  note "Never set 'platform: linux/amd64' — emulation jitter makes a 200 ms threshold flap."

  DISK_KB="$(df -k / | tail -1 | awk '{print $4}')"
  DISK_GB=$((DISK_KB / 1024 / 1024))
  if [[ "$DISK_GB" -lt 3 ]]; then
    warn "free disk" "${DISK_GB} GB — images need roughly 600 MB plus build scratch"
  else
    pass "free disk" "${DISK_GB} GB"
  fi
else
  fail "daemon reachable" "cannot connect to the Docker socket"
  note "Docker Desktop is not running  ->  open -a Docker   (then wait ~30s and re-run)"
  note "Everything below this line still runs; 'make test-unit' works with the daemon off."
fi

# --- ports ------------------------------------------------------------------
printf '\n%sports%s\n' "$C_DIM" "$C_OFF"

# 6379 is EXPECTED to be occupied by the developer's native redis-server. That
# is precisely why our container publishes on 6380. Reporting it as an error
# would train the reader to ignore doctor output. (ADR-004)
if port_in_use 6379; then
  warn "6379 (native redis)" "in use by '$(port_owner 6379)' — expected, not a problem"
  note "Ours publishes on 6380. Host commands need 'redis-cli -p 6380'."
else
  pass "6379" "free (no native redis running)"
fi

for spec in "8000:proxy" "6380:redis (ours)" "9001:us-east-1" "9002:eu-west-1" "9003:ap-southeast-1"; do
  port="${spec%%:*}"
  label="${spec#*:}"
  if port_in_use "$port"; then
    owner="$(port_owner "$port")"
    if [[ "$owner" == "com.docke"* || "$owner" == "docker"* ]]; then
      pass "$port ($label)" "held by Docker — the stack is probably already up"
    else
      fail "$port ($label)" "in use by '$owner'"
      note "Stop that process or the stack cannot bind $port."
    fi
  else
    pass "$port ($label)" "free"
  fi
done

# --- workspace --------------------------------------------------------------
printf '\n%sworkspace%s\n' "$C_DIM" "$C_OFF"

if [[ -f "$ROOT/.env" ]]; then
  pass ".env" "present"
else
  if cp "$ROOT/.env.example" "$ROOT/.env" 2>/dev/null; then
    pass ".env" "created from .env.example"
  else
    fail ".env" "missing and could not be created from .env.example"
  fi
fi

case "$ROOT" in
  *[[:space:]]*|*"&"*)
    warn "project path" "contains spaces and/or '&'"
    note "Handled: compose project name is pinned to 'mrfp', scripts quote every"
    note "expansion, and the chaos harness uses argv lists rather than a shell."
    ;;
  *) pass "project path" "no shell-hostile characters" ;;
esac

if [[ -d "$ROOT/.git" ]]; then
  pass "git repo" "$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'detached')"
else
  warn "git repo" "not initialised"
fi

# --- verdict ----------------------------------------------------------------
printf '\n'
if [[ "$FAILED" -gt 0 ]]; then
  printf '%s%d check(s) failed%s, %d warning(s). Fix the FAIL lines above.\n\n' \
    "$C_FAIL" "$FAILED" "$C_OFF" "$WARNED"
  exit 2
fi

printf '%sReady%s — %d warning(s), none blocking.\n\n' "$C_PASS" "$C_OFF" "$WARNED"
