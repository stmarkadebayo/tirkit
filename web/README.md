# Tirkit web UI

React + TypeScript + Vite single-page app, served as static files by the Tirkit server.

## What belongs here

| Path | Contents |
| --- | --- |
| `src/api/index.ts` | `fetch` wrapper (cookies, CSRF) and the named API types |
| `src/api/schema.ts` | **Generated** from `docs/api/openapi.json`. Do not edit by hand |
| `src/pages/` | One component per screen |
| `src/components/` | Shared components (content rendering, charts, dataset dialogs) |
| `scripts/gen-api.mjs` | Generates `src/api/schema.ts` (`npm run api`) |

## Rules

- The UI talks to the server **only through the HTTP API**. Response types come from the generated
  schema; do not hand-write them. If the UI needs a field the API does not return, change the
  server's models first (`src/tirkit/server/models.py`), run `make api`, and let `tsc` show what to
  update.
- Never import anything from the Python package or depend on server internals.

## Run only this area

```bash
npm ci
npm run dev          # Vite on :5173, proxying /api to a server on :8000 (make serve)
npm run typecheck
npm run api          # regenerate src/api/schema.ts after the server schema changes
make test-web        # from the repo root: generated types current + typecheck
```
