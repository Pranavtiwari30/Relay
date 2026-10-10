# Relay task runner. `make help` lists targets.
#
# Everything here is local and safe. No target creates, changes or destroys
# cloud resources; deployment is described in docs/deployment/.

SHELL := /bin/bash
UVRUN := uv run --env-file .env
PY_SRC := packages/relay_core/relay_core services/api/relay_api services/worker/relay_worker

.DEFAULT_GOAL := help
.PHONY: help setup install db-up db-down db-reset migrate seed dev dev-api dev-worker dev-web \
        test test-unit test-integration test-e2e test-web test-web-e2e coverage lint fmt typecheck \
        audit check evals mcp openapi tf-check clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- setup -----------------------------------------------------------------

setup: install ## First-time setup: dependencies, .env with generated secrets, database, demo data
	@uv run python scripts/setup_env.py
	@$(MAKE) --no-print-directory db-up migrate seed
	@echo "Ready. Run 'make dev', then open http://localhost:5173"

install: ## Install Python and frontend dependencies from lockfiles
	uv sync --frozen
	cd apps/web && npm ci

db-up: ## Start the project-local PostgreSQL cluster (port 54329)
	@scripts/local-postgres.sh start

db-down: ## Stop the project-local PostgreSQL cluster
	@scripts/local-postgres.sh stop

db-reset: ## Delete and recreate the local cluster (destroys local dev data only)
	@scripts/local-postgres.sh reset

migrate: ## Apply database migrations to the dev database
	$(UVRUN) relay-migrate upgrade

seed: ## Create the demo workspace, accounts and integration (idempotent)
	$(UVRUN) python scripts/seed.py

# --- run -------------------------------------------------------------------

dev: ## Run API (:8000), worker (:8001) and web UI (:5173) together
	@scripts/dev.sh

dev-api: ## Run only the API
	$(UVRUN) uvicorn relay_api.main:app_factory --factory --host 127.0.0.1 --port 8000

dev-worker: ## Run only the worker (with local dispatcher and queue loops)
	$(UVRUN) uvicorn relay_worker.main:app_factory --factory --host 127.0.0.1 --port 8001

dev-web: ## Run only the web UI dev server
	cd apps/web && npm run dev

mcp: ## Run the read-only MCP server over stdio (needs RELAY_MCP_WORKSPACE_ID)
	$(UVRUN) relay-mcp

# --- quality gates ---------------------------------------------------------

test: db-up ## Run every Python test (needs the local database)
	uv run pytest

test-unit: ## Run tests that need no database
	uv run pytest tests/unit tests/security/test_edge_defences.py

test-integration: db-up ## Run database-backed tests
	uv run pytest tests/integration tests/contract tests/security

test-e2e: db-up ## Run the real-socket end-to-end tests (slower)
	uv run pytest tests/e2e

coverage: db-up ## Tests with coverage; fails under 80% on relay_core
	uv run pytest --cov --cov-report=term-missing:skip-covered
	uv run coverage report --include='packages/relay_core/*' --fail-under=80 > /dev/null

test-web: ## Frontend unit tests
	cd apps/web && npm test

test-web-e2e: ## Browser end-to-end tests (starts its own API/worker/UI on spare ports)
	cd apps/web && npm run e2e

lint: ## Lint and format-check Python and frontend
	uv run ruff format --check .
	uv run ruff check .
	cd apps/web && npm run lint

fmt: ## Auto-format Python and frontend
	uv run ruff format .
	uv run ruff check --fix .
	cd apps/web && npm run format

typecheck: ## Static type checks (mypy strict, tsc strict)
	uv run mypy $(PY_SRC)
	cd apps/web && npm run typecheck

audit: ## Dependency vulnerability scans
	uv export --frozen --no-dev --no-emit-workspace --format requirements-txt -o /tmp/relay-requirements.txt -q
	uv run pip-audit -r /tmp/relay-requirements.txt --disable-pip
	cd apps/web && npm audit --omit=dev --audit-level=high

check: lint typecheck coverage test-web ## Everything CI runs on a pull request
	cd apps/web && npm run build

evals: db-up ## Run the investigation evals in deterministic (model-free) mode
	uv run python -m evals.run_evals

openapi: ## Write the OpenAPI document to docs/design/openapi.json
	uv run python scripts/export_openapi.py

tf-check: ## Format-check and validate Terraform (no cloud access, no state)
	cd infra/gcp && terraform fmt -check -recursive && terraform init -backend=false -input=false && terraform validate

clean: ## Remove caches and build output (keeps .env and the database)
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov apps/web/dist apps/web/test-results apps/web/playwright-report
	find . -name __pycache__ -not -path './.venv/*' -not -path './apps/web/node_modules/*' -prune -exec rm -rf {} +
