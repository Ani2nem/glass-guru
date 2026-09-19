VENV := .venv
PY   := $(VENV)/bin/python
GG   := $(VENV)/bin/glass-guru

.PHONY: help install test lint fmt typecheck check board scenario scenarios snapshots \
        params world travel osrm-setup osrm-up freeze-travel geocode clean

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install the project with dev extras
	python3.12 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip setuptools wheel
	$(PY) -m pip install -e ".[dev]"

test:  ## Run the test suite
	$(PY) -m pytest tests -q

lint:  ## Lint without modifying files
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests

fmt:  ## Auto-fix lint and formatting
	$(VENV)/bin/ruff check --fix src tests
	$(VENV)/bin/ruff format src tests

typecheck:  ## Strict type check
	$(VENV)/bin/mypy

check: lint typecheck test  ## Everything CI will eventually gate on

board:  ## Render a day's schedule, e.g. make board DATE=2026-09-21
	$(GG) solve $(if $(DATE),--date $(DATE),)

scenario:  ## Run one disruption scenario, e.g. make scenario NAME=van_breakdown
	$(GG) scenario $(NAME)

scenarios:  ## List the available scenarios
	$(GG) scenario list

osrm-setup:  ## One-time: download and prepare the OSRM road network (~69MB)
	./scripts/setup_osrm.sh

osrm-up:  ## Start the local OSRM backend
	docker compose up -d osrm

travel:  ## Compare synthetic, frozen and live OSRM travel times
	$(GG) travel --compare

freeze-travel:  ## Re-freeze the committed travel snapshot from live OSRM
	$(PY) scripts/freeze_travel.py

geocode:  ## Re-resolve fixture addresses and report coordinate drift
	$(PY) scripts/geocode_fixture.py

snapshots:  ## Regenerate golden board snapshots (review the diff before committing)
	UPDATE_SNAPSHOTS=1 $(PY) -m pytest tests/unit/test_cli_snapshots.py -q
	@git diff --stat tests/snapshots || true

params:  ## Show business parameters and their provenance
	$(GG) params

world:  ## Show the sample business: workers, vans, jobs
	$(GG) show

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache
