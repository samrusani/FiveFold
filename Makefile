# Common tasks (0007). Run `make help` for the list.

UV ?= uv
FIVEFOLD_ADMIN_DSN ?= postgresql://postgres@127.0.0.1:5432/postgres
FIVEFOLD_TEMPORAL_CLI ?= $(CURDIR)/.temporal/temporal
export FIVEFOLD_ADMIN_DSN FIVEFOLD_TEMPORAL_CLI

.DEFAULT_GOAL := help
.PHONY: help setup temporal-setup check-temporal tla-setup check-tla tla-tables tla-ci tla-nightly fmt lint typecheck test test-fast test-pg check db-up db-down db-reset demo \
	viewer-build viewer viewer-e2e clean

help: ## List the targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup: ## Install the locked dependencies into .venv
	$(UV) sync --locked

temporal-setup: ## Install the checksum-verified Temporal CLI for B1 tests
	PYTHONPATH=src python3 scripts/temporal_cli.py install

check-temporal: ## Check the Temporal CLI version required by B1
	PYTHONPATH=src python3 scripts/temporal_cli.py check

tla-setup: ## Install the checksum-verified TLC jar (0021)
	python3 scripts/tla_tools.py install

check-tla: ## Check Java 17 and the verified TLC jar
	python3 scripts/tla_tools.py check

tla-tables: ## Refresh the readable TLC contract tables from Python
	$(UV) run python tla/check_model.py tables

tla-ci: check-tla ## Check the bounded gateway model with TLC
	$(UV) run python tla/check_model.py ci

tla-nightly: check-tla ## Check the larger gateway model with TLC
	$(UV) run python tla/check_model.py nightly

fmt: ## Format the code and apply safe lint fixes
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint: ## Check formatting and lint rules
	$(UV) run ruff format --check .
	$(UV) run ruff check .

typecheck: ## Run mypy in strict mode
	$(UV) run mypy

test: ## Run every test (needs PostgreSQL; see db-up)
	$(UV) run pytest

test-fast: ## Run the tests that need no database
	$(UV) run pytest -m "not pg"

test-pg: ## Run only the tests that need PostgreSQL
	$(UV) run pytest -m pg

check: check-temporal tla-ci ## Lint, type check and run the bounded model and every test
	$(MAKE) lint typecheck test

db-up: ## Start PostgreSQL 16 on 127.0.0.1:5432 (Docker Compose)
	docker compose up --detach --wait postgres

db-down: ## Stop the local database
	docker compose down

db-reset: ## Recreate the fivefold_dev database, then create roles and migrate
	$(UV) run python -m fivefold.harness.migrations reset --database fivefold_dev

demo: ## Run the demos of record 0006 (1, 2a, 2b, 3) for B0 and G2 from a seed (needs PostgreSQL)
	$(UV) run python -m fivefold.harness.demo

viewer-build: ## Install the viewer's locked npm packages and build its frontend (needs Node 26)
	cd viewer && npm ci && npm run build

viewer: ## Serve the viewer on http://127.0.0.1:8300 (run viewer-build first)
	$(UV) run python -m fivefold.viewer

viewer-e2e: ## Check the served viewer in Chromium and save a screenshot (needs a running viewer)
	cd viewer && npm run e2e

clean: ## Remove caches
	rm -rf .mypy_cache .ruff_cache .pytest_cache .hypothesis
	find src tests -name __pycache__ -type d -prune -exec rm -rf {} +
