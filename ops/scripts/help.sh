#!/usr/bin/env bash
set -euo pipefail

if [[ -t 1 ]]; then B=$'\033[1m'; D=$'\033[2m'; O=$'\033[0m'; else B=''; D=''; O=''; fi

printf '%s\n' \
"" \
"${B}Multi-Region Model Deployment & Failover Proxy${O}" \
"${D}The Makefile is the only supported surface for lifecycle, build and test steps.${O}" \
"" \
"${B}getting started${O}" \
"  make doctor       preflight: daemon, ports, disk, arch, make version" \
"  make test-unit    unit tests — works with the Docker daemon STOPPED" \
"  make build        build both images (cold build: 3-6 min)" \
"  make up           start the stack and wait for healthy" \
"" \
"${B}lifecycle${O}" \
"  make up-regions   regions + redis only, without the proxy" \
"  make down         stop and remove containers" \
"  make ps           what is running" \
"  make health       one line per region: healthy?, breaker state, last error" \
"  make logs         follow proxy logs (JSON)" \
"" \
"${B}chaos${O}" \
"  make chaos        the brief's scenario: flood + docker stop + recovery" \
"  make chaos-NAME   one scenario, e.g. make chaos-pause-us-east" \
"  make report       regenerate the AUTOGEN blocks in docs/CHAOS-REPORT.md" \
"  make bench        ApacheBench throughput cross-check" \
"" \
"${B}quality${O}" \
"  make test         full suite (needs the stack up)" \
"  make lint         ruff check" \
"  make fmt          ruff format" \
"  make docs-check   fail on TODOs or measurements outside AUTOGEN blocks" \
"" \
"${B}housekeeping${O}" \
"  make venv         create/refresh the host virtualenv" \
"  make reset        flush app rate-limit keys (db 0 only, never FLUSHALL)" \
"  make clean        remove .venv, caches and chaos artifacts" \
"" \
"${D}Docs: README.md · docs/RUNBOOK.md · docs/CONTRACT.md   Build order: CLAUDE.md section 9${O}" \
""
