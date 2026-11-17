# Self-hosting

`docker compose up -d` starts three services: Postgres, the server (API, OTLP ingest, UI) and the
worker (UI experiments, online evals, alerts, retention).

| Variable | Purpose |
| --- | --- |
| `POSTGRES_PASSWORD` | Database password (set it before exposing the server) |
| `TIRKIT_PORT` | Host port (default 8000) |
| `TIRKIT_INSECURE_COOKIES` | `1` for plain HTTP; remove it when serving over HTTPS |
| `LLM_BASE_URL`, `LLM_API_KEY` | OpenAI-compatible endpoint for playground, UI experiments and judges |
| `ANTHROPIC_API_KEY` | Enables `anthropic/<model>` in the playground and judges |
| `TIRKIT_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM` | Email alerts |
| `TIRKIT_ADMIN_EMAIL`, `TIRKIT_ADMIN_PASSWORD` | Create the first admin at startup instead of the setup page |

**HTTPS.** Put a reverse proxy (Caddy, nginx, a cloud load balancer) in front and remove
`TIRKIT_INSECURE_COOKIES` so session cookies are `Secure`.

**Data.** Traces, scores and datasets live in Postgres; media files in the `media` volume. Set a
retention period per project in **Settings**; the worker deletes older traces hourly. Back up with
`tirkit backup` and practise restoring with the drill: see [Operations](operations.md).

**Kubernetes.** A Helm chart is in `deploy/helm/tirkit`: see [Operations](operations.md#kubernetes-helm).

**Monitoring.** Prometheus metrics are served at `/metrics` (`TIRKIT_METRICS_TOKEN` to protect them,
`TIRKIT_METRICS=0` to turn them off).

**Single process.** `pip install "tirkit[server]" && tirkit serve --database-url tirkit.db` runs the API,
UI and an embedded worker on SQLite, for evaluation and small teams.

**Scale.** Measured on a laptop: 1,679–3,105 spans/s sustained ingestion with nothing dropped, and
trace-list queries at most 83 ms p95 with 1 million spans (see README Results for the method).

## Security

- API keys and passwords are argon2-hashed; secrets are shown once.
- Every project-scoped query is bound to the caller's project; API keys work only for their project.
- Sessions use HttpOnly cookies; state-changing UI requests need a CSRF token.
- Uploaded media is served with `Content-Security-Policy: sandbox` and `nosniff`.
- PII redaction can run at ingest, per project, with custom patterns.
- Report vulnerabilities privately (see `SECURITY.md`).
