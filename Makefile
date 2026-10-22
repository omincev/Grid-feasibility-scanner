.PHONY: setup test test-db lint contracts migrate migrate-local dev export-static fmt up down \
	load-curated ingest snapshot features scores valuation export pipeline

setup:
	uv sync --all-groups

test:
	uv run pytest

test-db:
	uv run pytest -m db

lint:
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts
	uv run mypy

fmt:
	uv run ruff check --fix src tests scripts
	uv run ruff format src tests scripts

contracts:
	uv run python -m scanner.core.contracts

# dbmate (https://github.com/amacneil/dbmate) against $DATABASE_URL
migrate:
	dbmate --migrations-dir db/migrations --no-dump-schema up

# Fallback without dbmate; same schema_migrations table
migrate-local:
	uv run python scripts/migrate_local.py "$$DATABASE_URL"

dev:
	uv run dagster dev -w workspace.yaml

# Local stack (infra/README.md). `down` never removes volumes.
up:
	docker compose up -d db postgrest

down:
	docker compose --profile orchestrator --profile s3 down

# Thin aliases for the Dagster-independent CLI
load-curated ingest snapshot features scores valuation export pipeline:
	uv run python -m scanner.cli $@

export-static:
	uv run python -m scanner.runtime.export
