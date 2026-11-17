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
| `GET /api/projects/{id}/traces/{trace_id}` | Trace with spans and scores |
| `GET /api/projects/{id}/metrics` | Dashboard series (`since`, `until`, `buckets`, `group_by`): tokens, cost, latency, cache hit rate, reasoning tokens, time to first token |
| `GET /api/projects/{id}/compare?baseline=&candidate=` | Paired comparison with CIs |
| `POST /api/projects/{id}/experiments/run` | Start a prompt × model × dataset experiment |
| `GET /api/projects/{id}/export/traces.jsonl` | Export spans |

Outside the JSON API: `GET /api/health` (liveness and database check) and `GET /metrics`
(Prometheus text format; see [Operations](operations.md#monitoring)).
