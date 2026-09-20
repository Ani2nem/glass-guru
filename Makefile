VENV := .venv
PY   := $(VENV)/bin/python
GG   := $(VENV)/bin/glass-guru

.PHONY: help install test lint fmt typecheck check board scenario scenarios snapshots \
        image image-run scorecard tf-bootstrap tf-app tf-check \
        params world travel osrm-setup osrm-up freeze-travel geocode demo mcp trace \
        eval eval-offline eval-baseline load aws-check web-install web-build web-check api dev clean

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

eval:  ## Run the eval suite (model tiers skip without credentials)
	GLASS_GURU_TRAVEL=frozen $(VENV)/bin/glass-guru-eval $(ARGS)

eval-offline:  ## Run only the tiers that need no model
	GLASS_GURU_TRAVEL=frozen $(VENV)/bin/glass-guru-eval --tier 0 --tier 3

load:  ## Measure where the solver stops coping
	$(PY) -m glass_guru.evals.load

eval-baseline:  ## Record the current scores as the regression baseline
	GLASS_GURU_TRAVEL=frozen $(VENV)/bin/glass-guru-eval --update-baseline

image:  ## Build the container image locally (linux/arm64, as deployed)
	docker build --platform linux/arm64 -t glass-guru:dev .

image-run:  ## Run the built image and print what its health endpoints say
	@docker rm -f glass-guru-local >/dev/null 2>&1 || true
	docker run -d --name glass-guru-local -p 8000:8000 glass-guru:dev
	@until curl -sf http://127.0.0.1:8000/api/health >/dev/null; do sleep 1; done
	@echo "health : $$(curl -s http://127.0.0.1:8000/api/health)"
	@echo "ready  : $$(curl -s http://127.0.0.1:8000/api/ready)"
	@echo "board  : http://127.0.0.1:8000"

scorecard:  ## Render the eval scorecard CI posts on a pull request
	@GLASS_GURU_TRAVEL=frozen $(VENV)/bin/glass-guru-eval $(ARGS) --json /tmp/gg-eval.json >/dev/null
	@$(PY) -m glass_guru.evals.scorecard /tmp/gg-eval.json --baseline evals/baseline.json

tf-bootstrap:  ## Plan the one-time bootstrap stack (OIDC, roles, registry, state)
	terraform -chdir=infra/bootstrap init -input=false
	terraform -chdir=infra/bootstrap plan

tf-app:  ## Plan the application stack. IMAGE=<url>@sha256:<digest> required
	@test -n "$(IMAGE)" || (echo "IMAGE=<repo-url>@sha256:<digest> required - see infra/app/README.md"; exit 1)
	terraform -chdir=infra/app init -input=false \
	  -backend-config="bucket=$$(terraform -chdir=infra/bootstrap output -raw state_bucket)"
	terraform -chdir=infra/app plan -var "image=$(IMAGE)"

tf-check:  ## Validate and format-check both stacks
	terraform -chdir=infra/bootstrap fmt -check
	terraform -chdir=infra/bootstrap validate
	terraform -chdir=infra/app fmt -check
	terraform -chdir=infra/app validate

aws-check:  ## Verify AWS credentials and Bedrock model access
	@echo "--- identity ---"
	@aws sts get-caller-identity || (echo "no credentials: see docs/aws-setup.md"; exit 1)
	@echo "--- nova-lite access in $${AWS_REGION:-us-east-1} ---"
	@aws bedrock list-foundation-models --region $${AWS_REGION:-us-east-1} \
	  --query "modelSummaries[?contains(modelId,'nova-lite')].modelId" --output text \
	  || (echo "cannot list models: check the policy in docs/aws-setup.md"; exit 1)
	@echo "--- end to end ---"
	$(GG) triage "Dan called, van 3 won't start"

web-install:  ## Install the dispatch board's dependencies (once)
	cd web && npm install

web-build:  ## Build the board; the API then serves it from web/dist
	cd web && npm run build

web-check:  ## Type-check the board
	cd web && npm run typecheck

api:  ## Run the API (serves the built board at http://127.0.0.1:8000)
	GLASS_GURU_TRAVEL=$${GLASS_GURU_TRAVEL:-frozen} $(VENV)/bin/glass-guru-api

dev:  ## Run the API and the board's dev server together, with hot reload
	@echo "API   http://127.0.0.1:8000"
	@echo "Board http://127.0.0.1:5173  (proxies /api to the API)"
	@( GLASS_GURU_TRAVEL=$${GLASS_GURU_TRAVEL:-frozen} \
	   $(PY) -m uvicorn glass_guru.api.main:app --reload --port 8000 & \
	   cd web && npm run dev; kill %1 )

mcp:  ## Run the MCP server over stdio
	$(VENV)/bin/glass-guru-mcp

trace:  ## Run a command with spans printed to the console, e.g. make trace CMD=commit
	GLASS_GURU_TRACE_CONSOLE=1 $(GG) $(CMD)

demo:  ## End-to-end Gate 3 walkthrough in a throwaway workspace
	@rm -rf /tmp/glass-guru-demo
	$(GG) --workspace /tmp/glass-guru-demo init
	$(GG) --workspace /tmp/glass-guru-demo commit
	$(GG) --workspace /tmp/glass-guru-demo event job-confirmed j-402 \
	  --window-start 09:00 --window-end 15:00 --commitment-cost 250
	$(GG) --workspace /tmp/glass-guru-demo event job-dispatched j-401 --at 06:05
	$(GG) --workspace /tmp/glass-guru-demo event van-unavailable van-1 --at 10:40 --reason "wont start"
	$(GG) --workspace /tmp/glass-guru-demo repair
	$(GG) --workspace /tmp/glass-guru-demo repair --apply
	$(GG) --workspace /tmp/glass-guru-demo diff

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
