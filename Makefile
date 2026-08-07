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
        test-unit test lint fmt docs-check chaos report bench reset clean

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

test:
	@bash ops/scripts/_todo.sh 7 "make test"

lint: venv
	@$(RUFF) check .

fmt: venv
	@$(RUFF) format .

docs-check:
	@bash ops/scripts/_todo.sh 8 "make docs-check"

# --- chaos ------------------------------------------------------------------

chaos:
	@bash ops/scripts/chaos.sh stop-us-east

# One named scenario, e.g. `make chaos-pause-us-east`.
chaos-%:
	@bash ops/scripts/chaos.sh "$*"

report:
	@bash ops/scripts/_todo.sh 8 "make report"

bench:
	@bash ops/scripts/_todo.sh 5 "make bench"

# --- housekeeping -----------------------------------------------------------

reset:
	@bash ops/scripts/_todo.sh 4 "make reset"

clean:
	@bash ops/scripts/clean.sh
