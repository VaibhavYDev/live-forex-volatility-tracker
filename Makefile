.DEFAULT_GOAL := help
SHELL := /bin/bash

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.env:
	@cp .env.example .env && echo "created .env from .env.example"

install: .env ## Install Python and Node dependencies
	uv sync --all-packages
	cd apps/web && npm install

lint: ## ruff + mypy --strict + tsc
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy packages apps
	cd apps/web && npx tsc --noEmit

fmt: ## Auto-fix formatting and lint
	uv run ruff format .
	uv run ruff check . --fix

test: ## Unit + integration + chaos
	uv run pytest tests -q --timeout=60

test-unit: ## Pure domain logic only (no services needed)
	uv run pytest tests/unit -q

test-chaos: ## Fault injection suite
	uv run pytest tests/chaos -q -v --timeout=60

test-ci: ## Exactly what CI runs: services required, not skipped
	FX_REQUIRE_SERVICES=1 uv run pytest tests -q --timeout=90 --cov --cov-report=term-missing

test-cov: ## Coverage across all four Python packages (gated at 60%)
	uv run pytest tests -q --cov --cov-report=term-missing

bench: ## Throughput and backpressure benchmarks (prints real numbers)
	uv run pytest tests/load -q -s -m load --timeout=180

deps: .env ## Start only Redis + TimescaleDB (for local dev)
	docker compose up -d redis timescale

dev: deps ## Run all services locally with hot reload
	@echo "→ api      http://localhost:8000/docs"
	@echo "→ web      http://localhost:5173"
	@trap 'kill 0' EXIT; \
	  uv run python -m fx_ingestor.main & \
	  uv run python -m fx_worker.main & \
	  uv run uvicorn fx_api.main:app --reload --port 8000 & \
	  (cd apps/web && npm run dev) & \
	  wait

up: .env ## Full stack in Docker
	docker compose up --build

observe: .env ## Full stack + Prometheus + Grafana
	docker compose --profile observability up --build

down: ## Stop everything
	docker compose --profile observability down

clean: ## Stop and delete volumes (destroys stored bars)
	docker compose --profile observability down -v

.PHONY: help install lint fmt test test-unit test-chaos test-cov bench deps dev up observe down clean demo-data demo demo-verify

demo-data: ## Rebuild the published demo dataset from the real engine
	uv run python scripts/build_demo_dataset.py apps/web/src/demo/dataset.json

demo: ## Rebuild docs/ — the single-file bundle behind the public link
	cd apps/web && VITE_DEMO=1 npm run build
	node scripts/inline_demo.mjs
	$(MAKE) demo-verify

demo-verify: ## Load docs/index.html under every path shape a host might use
	node scripts/verify_demo.mjs
