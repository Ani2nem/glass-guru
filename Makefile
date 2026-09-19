VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: help install test lint fmt typecheck check scenario dev clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install the project with dev extras
	python3.12 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip setuptools wheel
	$(PY) -m pip install -e ".[dev]"

test: ## Run the test suite
	$(PY) -m pytest tests -q

lint: ## Lint without modifying files
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests

fmt: ## Auto-fix lint and formatting
	$(VENV)/bin/ruff check --fix src tests
	$(VENV)/bin/ruff format src tests

typecheck: ## Strict type check
	$(VENV)/bin/mypy

check: lint typecheck test ## Everything CI will eventually gate on

scenario: ## Run one golden scenario, e.g. make scenario NAME=van_breakdown
	$(PY) -m glass_guru.evals.run_scenario $(NAME)

dev: ## Run the API and dispatch board locally
	$(PY) -m uvicorn glass_guru.api.main:app --reload --port 8000

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache
