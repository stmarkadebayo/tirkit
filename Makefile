PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup lint fmt test progress demo web serve dev-seed docker bench

setup:
	$(PY) -m venv $(VENV) && $(BIN)/pip install -U pip && $(BIN)/pip install -e ".[dev]"

lint:
	$(BIN)/ruff check . && $(BIN)/mypy src

fmt:
	$(BIN)/ruff format . && $(BIN)/ruff check --fix .

test:
	$(BIN)/pytest -q

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
