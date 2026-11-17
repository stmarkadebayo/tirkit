# HTTP API

The full, interactive reference is served by every Tirkit server at **`/api/docs`** (OpenAPI schema
at `/api/openapi.json`). The same schema is checked into the repository as
[`docs/api/openapi.json`](https://github.com/stmarkadebayo/tirkit/blob/main/docs/api/openapi.json):
use it to generate a client in any language. Every request and response has a schema.

Errors always have the shape `{"detail": "..."}`, including invalid requests (status 400).

Authentication:

- **API key** (SDKs, exporters, CI): HTTP Basic with the public key as user name and the secret key
  as password. Keys are scoped to one project.
- **Session** (UI): `POST /api/auth/login`, then send the `X-CSRF-Token` header (value of the
  `tirkit_csrf` cookie) on state-changing requests.

Main endpoints:

| Method and path | Purpose |
| --- | --- |
| `POST /api/v1/ingest` | Batch of spans (`{"spans": [...]}`) |
| `POST /v1/traces` | OTLP/HTTP traces (protobuf or JSON) |
| `PUT /api/v1/media/{sha256}` | Upload media by content hash |
| `POST /api/v1/scores` | Score or end-user feedback on a trace |
| `GET /api/v1/prompts/{name}?label=` | Fetch a prompt version |
| `GET /api/projects/{id}/traces` | Filtered trace list (`name`, `user_id`, `session_id`, `release`, `search`, `error`, `start_after`, `min_duration_ms`, ...) |
| `GET /api/projects/{id}/traces/{trace_id}` | Trace, scores and a light span tree: ids, parents, timings, tokens, cost and short input/output previews. `limit`, `offset` and `preview` page it; `full=1` returns every span in full |
| `GET /api/projects/{id}/traces/{trace_id}/spans/{span_id}` | One span in full (input, output, parameters, attributes) |
| `GET /api/projects/{id}/metrics` | Dashboard series (`since`, `until`, `buckets`, `group_by`) |
| `GET /api/projects/{id}/compare?baseline=&candidate=` | Paired comparison with CIs |
| `POST /api/projects/{id}/experiments/run` | Start a prompt × model × dataset experiment |
| `GET /api/projects/{id}/export/traces.jsonl` | Export spans |

**Large traces.** The trace endpoint returns up to 10,000 spans per page, oldest first, with
`total_spans`, `omitted` (spans after this page) and `next_offset` (null on the last page). Inputs
and outputs come as `input_preview` and `output_preview` (2,000 characters; on big pages fewer, as
all previews of a page share a budget; `preview` in the response says how many each got), with
`input_truncated` and `output_truncated`. Fetch the full span from `.../spans/{span_id}`.

**Retries.** Ingestion is an idempotent upsert on `(project, span_id)`: sending a batch again leaves
span and trace counts, tokens and cost unchanged, so clients may retry any batch. Send `Retry-After`
with `429` or `503` and the SDKs wait that long.
