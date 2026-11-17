# `tirkit.server`: the HTTP API

The FastAPI app behind `tirkit serve`: auth, ingestion, queries, and the routes the web UI uses.

## What belongs here

| File | Contents |
| --- | --- |
| `app.py` | `create_app()`: per-app state, router wiring, error handlers, the static web UI |
| `deps.py` | Authentication (API keys, sessions, CSRF), the per-request `Ctx`, shared helpers |
| `models.py` | Request and response models: **the API contract** |
| `openapi.py` | Builds the OpenAPI document (`python -m tirkit.server.openapi`) |
| `routers/` | One module per domain: `system`, `projects`, `ingest`, `traces`, `datasets`, `experiments`, `prompts`, `annotation`, `online` |
| `settings.py` | Configuration from `TIRKIT_*` environment variables |

Routes stay thin: parse the request, check the role (`ctx.require("member")`), call the store or a
service module, and return `json_response(...)`. Business logic belongs in the service and eval
modules (`tirkit.annotation`, `tirkit.online`, `tirkit.runner`, ...), queries in `tirkit.store`.

## Rules

- This is the delivery layer: it may import anything in `tirkit`, but **nothing outside
  `tirkit.server` and `tirkit.cli` may import FastAPI, Starlette or uvicorn**, and nothing may import
  `tirkit.server`. `make lint` enforces both (`.importlinter`).
- Every route declares a `response_model` from `models.py` and documents its query parameters.
  `tests/test_api_contract.py` fails if a route has no contract call or a response does not match.
- After changing a model or a route, run `make api` and commit `docs/api/openapi.json` and
  `web/src/api/schema.ts`. CI fails if either is stale.
- Do not run blocking work inside an `async def` route: use a plain `def` (FastAPI runs it in a
  thread) unless the route must read a raw body.

## Run only this area

```bash
make test-server     # server, API contract and (with TIRKIT_TEST_POSTGRES_URL) Postgres tests
make api-check       # schema and web types are current
make serve           # run it on :8000; the API reference is at /api/docs
```
