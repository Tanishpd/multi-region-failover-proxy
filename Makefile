# The only supported command surface for lifecycle, build, and test steps.
#
# ---------------------------------------------------------------------------
# WHY THIS FILE LOOKS LIKE THIS
#
# macOS ships GNU Make 3.81. `.SHELLFLAGS` arrived in 3.82 and is SILENTLY
# IGNORED below that — verified on this machine: a recipe containing
# `false | true` continued running and the target exited 0.
#
# Make checks the exit status of each recipe LINE, but a shell pipeline returns
# only its last command's status. So the rule here is:
#
#   * one command per recipe line
#   * no pipelines in recipes — anything with a `|` lives in ops/scripts/*.sh,
#     which carry their own `set -euo pipefail`
#
# Without this, `make chaos` would report success on a failed run, which would
# break the project's central claim. (ADR-016)
# ---------------------------------------------------------------------------

SHELL := /bin/bash

# The project directory contains spaces and an "&", so Compose's derived project
# name would be mangled. docker-compose.yml sets `name: mrfp` too. (ADR-002)
export COMPOSE_PROJECT_NAME := mrfp

VENV    := .venv
PY      := $(VENV)/bin/python
PYTEST  := $(VENV)/bin/pytest
RUFF    := $(VENV)/bin/ruff

.DEFAULT_GOAL := help

.PHONY: help doctor venv build up up-regions down ps health logs \
        test-unit test lint fmt docs-check chaos report bench reset clean \
        prod-config prod-up prod-down capacity calibrate calibrate-check \
        redis-ha-up redis-ha-down redis-ha-drill redis-ha-e2e soak

# --- meta -------------------------------------------------------------------

help:
	@bash ops/scripts/help.sh

doctor:
	@bash ops/scripts/doctor.sh

venv:
	@bash ops/scripts/venv.sh

# --- lifecycle --------------------------------------------------------------
# `build` is separate from `up` on purpose: a cold build of two images plus
# pulls runs 3-6 minutes, and a health-wait deadline started at `up` time would
# expire during it and report a false failure.

build:
	@bash ops/scripts/build.sh

up:
	@bash ops/scripts/up.sh all

up-regions:
	@bash ops/scripts/up.sh regions

down:
	@bash ops/scripts/down.sh

ps:
	@bash ops/scripts/ps.sh

health:
	@bash ops/scripts/health.sh

logs:
	@bash ops/scripts/logs.sh

# --- tests ------------------------------------------------------------------
# test-unit is the ONLY target that must work with the Docker daemon stopped.
# It is documented as step 0 of the README so a reader gets a green result
# before touching any infrastructure.

test-unit: venv
	@$(PYTEST) -q -m unit

test: venv
	@$(PYTEST) -q

# BOTH checks, because CI runs both. `make lint` ran only `ruff check` while
# .github/workflows/ci.yml also ran `ruff format --check`, so local was green
# and CI was red for five consecutive pushes before anyone looked. A local gate
# that is weaker than the remote one is worse than no local gate: it actively
# certifies broken pushes.
lint: venv
	@$(RUFF) check .
	@$(RUFF) format --check .

fmt: venv
	@$(RUFF) format .

docs-check:
	@bash ops/scripts/docs_check.sh

# --- chaos ------------------------------------------------------------------

chaos:
	@bash ops/scripts/chaos.sh stop-us-east

# One named scenario, e.g. `make chaos-pause-us-east`.
chaos-%:
	@bash ops/scripts/chaos.sh "$*"

report: venv
	@$(PY) -m chaos.report

bench:
	@bash ops/scripts/bench.sh

capacity:
	@bash ops/scripts/capacity.sh

# Production-shaped traffic from a committed dataset: many tenants, a real
# max_tokens mix, streaming and buffered together, and a noisy neighbour that
# must be throttled WITHOUT touching anyone else.
soak:
	@bash ops/scripts/soak.sh

# Derive the timeouts from measurement rather than asserting them. `-check`
# exits non-zero when a configured timeout is below the measured requirement.
calibrate:
	@bash ops/scripts/calibrate.sh

calibrate-check:
	@bash ops/scripts/calibrate.sh check

# --- redis high availability -------------------------------------------------
# A real Sentinel topology (primary + replica + 3 sentinels), so the HA claim is
# drilled rather than asserted. Joins the demo stack's network; `make up` first.

redis-ha-up:
	@docker compose -f deploy/compose.redis-ha.yml up -d --wait

redis-ha-down:
	@docker compose -f deploy/compose.redis-ha.yml down

# Kills whichever node Sentinel currently calls primary and measures the blip.
redis-ha-drill:
	@bash ops/scripts/redis-ha-drill.sh

# The same kill, but under LIVE proxy traffic — the version that proves a
# promotion is invisible to a caller rather than merely survivable by a client.
redis-ha-e2e:
	@bash ops/scripts/redis-ha-e2e.sh

# --- housekeeping -----------------------------------------------------------

reset:
	@bash ops/scripts/reset.sh

clean:
	@bash ops/scripts/clean.sh

# --- production profile ------------------------------------------------------
# A standalone topology, never merged with docker-compose.yml. See
# deploy/compose.prod.yml for why an override file cannot express it.

prod-config:
	@bash ops/scripts/prod.sh config

prod-up:
	@bash ops/scripts/prod.sh up

prod-down:
	@bash ops/scripts/prod.sh down
