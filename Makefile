# Common tasks (0007). Run `make help` for the list.

UV ?= uv
FIVEFOLD_ADMIN_DSN ?= postgresql://postgres@127.0.0.1:5432/postgres
export FIVEFOLD_ADMIN_DSN

.DEFAULT_GOAL := help
.PHONY: help setup fmt lint typecheck test test-fast test-pg check db-up db-down db-reset demo clean

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

setup: ## Install the locked dependencies into .venv
	$(UV) sync --locked

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

check: lint typecheck test ## Lint, type check and run every test

db-up: ## Start PostgreSQL 16 on 127.0.0.1:5432 (Docker Compose)
	docker compose up --detach --wait postgres

db-down: ## Stop the local database
	docker compose down

db-reset: ## Recreate the fivefold_dev database, then create roles and migrate
	$(UV) run python -m fivefold.harness.migrations reset --database fivefold_dev

demo: ## Run demo 1 (a stub until milestone M1)
	@echo "Demo 1 (order placed, reply lost) arrives in milestone M1. See docs/plan.md."

clean: ## Remove caches
	rm -rf .mypy_cache .ruff_cache .pytest_cache .hypothesis
	find src tests -name __pycache__ -type d -prune -exec rm -rf {} +
