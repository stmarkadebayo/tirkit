# Working on `@tirkit/sdk`

Spans, OpenAI auto-instrumentation, feedback and prompts for Node.js (18+). It sends data to a
Tirkit server over the HTTP API with an API key.

## What belongs here

The Node.js client: span creation and context propagation, a batching exporter, OpenAI
instrumentation, `feedback()` and prompt fetching. Keep public names and behaviour in step with the
Python SDK (`src/tirkit/tracing.py`, `client.py`): same field names (`trace_id`, not `traceId`, on the
wire), same defaults.

## Rules

- Depend only on the public HTTP API (`/api/v1/ingest`, `/api/v1/scores`, `/api/v1/prompts/...`),
  documented in `docs/api/openapi.json`. Never on server internals or the web UI.
- Tracing must never throw into the host application and must not block it.
- No runtime dependencies beyond Node's standard library unless there is no alternative.

## Run only this area

```bash
npm ci
npm test             # builds, then runs node --test on test/
```
