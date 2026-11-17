PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup lint fmt test test-core test-server test-web progress api api-check demo web serve dev-seed docker bench

setup:
	$(PY) -m venv $(VENV) && $(BIN)/pip install -U pip && $(BIN)/pip install -e ".[dev]"

lint:
	$(BIN)/ruff check . && $(BIN)/mypy src && $(BIN)/lint-imports

fmt:
	$(BIN)/ruff format . && $(BIN)/ruff check --fix .

test:
	$(BIN)/pytest -q

test-core:  # everything that does not need the server extra
	$(BIN)/pytest -q -m "not server"

test-server:  # HTTP API, auth, ingestion, workers (needs the server extra)
	$(BIN)/pytest -q -m server

test-web:  # web UI: generated API types are current and the code typechecks
	cd web && npm run -s api:check && npm run -s typecheck && npm test --silent

api:  # regenerate docs/api/openapi.json and the web UI's types from the server models
	$(BIN)/python -m tirkit.server.openapi > docs/api/openapi.json
	cd web && npm run -s api

api-check: api  # fails if the checked-in schema or web types are stale
	git diff --exit-code -- docs/api/openapi.json web/src/api/schema.ts

progress:
	@$(BIN)/pytest -q -m acceptance --no-header -rN | tail -1

demo:
	$(BIN)/python examples/quickstart.py

web:
	cd web && npm ci --no-audit --no-fund && npm run build

serve:
	$(BIN)/tirkit serve --port 8000

dev-seed:
	$(BIN)/python examples/seed_dev.py --url http://127.0.0.1:8000

docker:
	docker compose up -d --build

bench:
	$(BIN)/python research/benchmarks/bench_tracing.py
