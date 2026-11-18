# Changelog

All notable changes to Tirkit are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Tirkit uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `make dev-seed` now fills every screen of the web UI with synthetic, development-only data: traces, sessions, users, prompts, a three-version dataset, experiments with a regression, a human-labelled annotation queue, judge-reliability reports, online evaluators and alerts. It is deterministic, safe to run twice and needs no network or model (the judge is scripted). See `examples/seed_dev.py`.

### Changed
- Code is organised in enforced layers (core, storage, SDK, evaluation, services, delivery); `make lint` runs the import-linter contracts. Shared types live in `tirkit.types` and are still importable from their old modules.
- `tirkit.store` is now a package and the server is split into per-domain routers; public imports are unchanged.
- Request validation errors return 400 with `{"detail": "..."}` like every other error.
- The web UI's API types are generated from the server's OpenAPI schema (`make api`).
- Repository layout: `SPEC.md` and `PLAN.md` moved to `docs/`; contributor and policy files (`CONTRIBUTING.md`, `CLA.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`) moved to `.github/`; `benchmarks/`, `experiments/` and `evals/` moved under `research/`; tool caches and the built docs site go to `.cache/`.
- `tirkit.otel` is a package (`tirkit.otel.mapping` holds the attribute tables); its public functions are unchanged. Instrumentation wrappers can now patch module-level functions and positional arguments (internal `CallSpec` options).
- Model price lookup also tries `mistral/`, `cohere/` and `vertex_ai/` prefixed names and strips Bedrock region prefixes (`us.`, `eu.`...).
- `GET /api/projects/{id}/traces/{trace_id}` now returns a light span tree (ids, parents, timings, tokens, cost, short previews, paged with an explicit `omitted` count); use `?full=1` for the previous shape or `.../spans/{span_id}` for one full span.
- Ingestion decodes, validates and writes off the event loop, so `/api/health` stays fast while a slow batch is written.
- SDK retries honour `Retry-After`, use jittered exponential backoff and stop after 30 s (Python and TypeScript); the Python exporter defaults to 5 retries instead of 3.
- The trace page draws only the rows in view and shows every span of a trace, including spans in parent cycles, self-parents and orphans (previously these disappeared); the span tree is built without quadratic copying.
- The LLM judge's hardened prompt (random delimiters, strict reply format) is off by default; `LLMJudge(hardened=True)` turns it on. The `suspect_injection` flag stays on in every mode.
- The web UI opens on a new **Overview**: judge reliability first (agreement with human labels and its interval, or "not measured yet" with the label count), then experiments and the CI gate state, then traffic. The old Dashboard is still under Observe. The navigation is grouped Observe, Evaluate, Build.
- **Judge reliability** is a readable result (headline kappa with its interval and a plain verdict, position and length bias as charts, label count, next step) instead of a nine-column table; the rubric editor is collapsed.
- The **Traces** list draws only the rows in view and loads 200 at a time, so 10,000 rows scroll smoothly; filters are tiered (search, time range, status, then "More filters") and live in the URL; `j`/`k`, Enter, `x` and `/` work from the keyboard.
- On phones the navigation is a menu button and a drawer; every page was checked at 375 px for page-level horizontal scroll.
- Settings shows the redaction mode as three choices with what each removes, the redaction counter and a note about SDK-side redaction.
- Web pages load on demand, which takes the charting library out of the first bundle.
- Judge reliability reports now include a 95% bootstrap interval for kappa (`kappa_ci_low`, `kappa_ci_high`); reports stored earlier show no interval.

### Added
- Complete OpenAPI schema: every route has request and response models; checked in as `docs/api/openapi.json` and kept current by CI.
- API contract test that calls every route and validates the response against the schema.
- `make test-core`, `make test-server`, `make test-web`, `make api`, `make api-check`.
- CODEOWNERS, area labels on pull requests, and CI split by area.

- Indexed full-text search (SQLite FTS5, Postgres tsvector/GIN): word-prefix matching, accent folding on SQLite, traces match on any span.
- Gemini auto-instrumentation (google-genai: text, vision, audio/TTS output, streaming, embeddings, images, async).
- OpenAI Realtime voice tracing: speech-to-speech turns with input/output audio, transcripts, tool calls and usage.
- Tracing SDK: `tirkit.span()`, `@tirkit.trace` (sync, async, generators), context propagation across asyncio and threads, trace-level session/user/release/tags.
- Background batch exporter with bounded queue, retries and drop counting; `tirkit.init()` local mode on SQLite.
- SQLite store with spans, trace summaries, filters, text search and per-project isolation.
- Media store: images and audio stored once by content hash, extracted from provider payloads.
- Price table for 5,500+ models covering tokens, cached tokens, audio seconds, characters and images.
- Auto-instrumentation for OpenAI (chat, responses, embeddings, images, speech-to-text, text-to-speech, any OpenAI-compatible server) and Anthropic (messages, raw streams, `messages.stream()`).
- PII redaction (emails, phones, Luhn-checked cards, API keys and secrets, IPs, IBANs), tested on a fixture of known PII strings.
- Datasets: JSONL/CSV IO, content-hash versions stored in SQLite, mining from traces with redaction and near-duplicate removal.
- Scorers: exact match, rule checks (regex, length, banned phrases, JSON validity and schema), function scorers, WER/CER for speech-to-text, TTS round-trip intelligibility, embedding similarity.
- LLM judge: versioned rubrics (correctness, faithfulness, relevance, toxicity...), JSON parsing with one retry and recorded failures, pairwise judging in both orders, cache, USD budget, image inputs; OpenAI-compatible and Anthropic judge models.
- RAG demo app producing multi-span traces (`examples/rag_demo.py`).
- Statistics: seeded percentile bootstrap CIs, paired bootstrap differences, Cohen's and quadratic-weighted kappa, Spearman.
- Experiments: `run()` with concurrency, retries and per-item error capture; stored outputs, scores, latency and cost; `compare()` with paired CIs; `gate()` per SPEC §6.3 (lower-is-better scorers handled).
- Markdown and HTML reports; PR-comment gate report.
- CLI: `tirkit run / compare / gate`, `tirkit dataset build / import / export / list`, `tirkit traces list / show`.
- Server (`tirkit serve`, FastAPI): setup and login with CSRF-protected sessions, projects, roles, argon2-hashed API keys, batch ingestion, OTLP/HTTP ingest (GenAI conventions and OpenInference), media upload and sandboxed serving, scores and end-user feedback, per-project ingest redaction and price overrides, trace/span/session/user queries, dashboard metrics, JSONL export, dataset and experiment APIs.
- Postgres store with connection pooling; set-based trace summaries; same test-suite on SQLite and Postgres.
- SDK server mode (`tirkit.init(host=...)`), media upload by content hash, `tirkit.feedback()`; OTLP export sink.
- Benchmarks for ingestion throughput and query latency at 1M spans.
- Web UI (React): setup and login, dashboard (traffic, latency percentiles, cost, tokens, error rate, scores, breakdowns), trace list with filters and export, trace waterfall with chat, image, audio and tool-call rendering, scores and annotations, sessions, users, settings (API keys with setup snippets, members, redaction, retention, judge budget, prices).
- Docker image and `docker compose` deployment with Postgres; OpenTelemetry SDK example; dev seed script.
- Evaluation UI: datasets (versions, edit, upload/download, build from traces), experiments with mean and 95% CI per scorer, side-by-side compare with CI bars, verdicts and per-item regressions, Markdown report.
- Human annotation: score configs (numeric, pass/fail, categories), annotation queues with locking, keyboard-driven labelling, label export for judge reports.
- SDK and CLI runs against a server (`TIRKIT_HOST`) store datasets and experiments there.
- Prompt management: versioned text and chat prompts with `{{variable}}` templates and model config, labels (`production`, `latest`, custom), SDK `tirkit.get_prompt()` with caching and stale fallback, spans linked to the prompt version via `prompt.use()`.
- Playground: edit a prompt, fill variables, run it on any OpenAI-compatible endpoint (including local LM Studio/Ollama) or Anthropic, see tokens, latency and cost, save as a version; replay any traced LLM span.
- Experiments started from the UI (prompt version × model × dataset × scorers) run by a Postgres/SQLite job queue; `tirkit worker`; embedded worker for single-process installs; retention job.
- Online evals: evaluators with trace filters, deterministic sampling, rule or LLM-judge scorers on server-configured models, daily judge budget per project with skipped counts.
- Alerts on error rate, p95 latency, cost, volume or a score average, with webhook (Slack-compatible) or email delivery and history.
- Judge reliability: reports from annotation-queue labels (Cohen's, weighted and pass/fail kappa, agreement, length bias, position-swap flip rate), stored per rubric version; `tirkit judge-report`.
- E1 protocol (`docs/E1.md`) and position-bias experiment script; hand-written 50-question QA set over the demo docs.
- TypeScript SDK (`sdk-ts`, zero runtime dependencies): spans with AsyncLocalStorage nesting, trace context, batch exporter with retries, OpenAI client instrumentation incl. streams, feedback, prompts; Node demo.
- Integrations: LangChain/LangGraph callback handler; Hugging Face transformers pipelines (local small models, speech-to-text, text-to-speech, vision) with media capture; OTLP export sink.
- E2 seeded-regression suite: 20 regressions and 20 neutral prompt variants gated against a baseline.
- Apache-2.0 licence, NOTICE, Contributor License Agreement and CLA bot.
- Contributing guide, Code of Conduct, security policy, issue and pull-request templates.
- Secret scanning (gitleaks) in CI.
- `tirkit backup` and `tirkit restore`: SQLite online backup or Postgres `pg_dump` (custom format, in an exported snapshot), media files listed and checked by SHA-256, and a manifest of row counts and content hashes that `restore` verifies. `scripts/restore_drill.py` (backup, wipe, restore, compare) runs in CI weekly.
- Prometheus metrics at `/metrics`: ingest requests by route, outcome and reason, spans accepted, ingest latency, ingest buffer depth, Postgres pool usage, job queue depth and age, judge spend. Optional bearer token (`TIRKIT_METRICS_TOKEN`); `TIRKIT_METRICS=0` turns them off. Counters are shared across `tirkit serve --workers N` processes.
- Helm chart (`deploy/helm/tirkit`): server and worker Deployments, optional Postgres, Secret, probes on `/api/health`, HPA, PodDisruptionBudget, non-root pods with a read-only root filesystem. CI runs `helm lint` and `kubeconform`.
- Operations guide (`docs/site/operations.md`): backups and the restore drill, monitoring, upgrades, retention, Kubernetes and measured sizing.
- OpenTelemetry parity: OTLP spans get the same cost, redaction, session, user and media handling as SDK spans. One mapping module (`tirkit/otel/mapping.py`) covers the GenAI conventions, OpenInference, OpenLLMetry and the Vercel AI SDK; tested with real OTel exporters (Python HTTP and gRPC, Node) and the OpenInference, OpenLLMetry and OpenTelemetry OpenAI instrumentations.
- Optional OTLP/gRPC receiver on its own port (`tirkit serve --otlp-grpc-port`, extra `tirkit[otlp-grpc]`), an OpenTelemetry Collector config (`examples/otel-collector.yaml`) and a "Bring your own OpenTelemetry" quickstart.
- Auto-instrumentation for Amazon Bedrock (boto3, aioboto3), Mistral, Cohere, Vertex AI (legacy `vertexai` SDK) and LiteLLM.
- Integrations for the OpenAI Agents SDK, LlamaIndex, DSPy and CrewAI (`tirkit.instrument("openai_agents" | "llamaindex" | "dspy" | "crewai")`).
- Span kinds `vector_db`, `mcp_tool`, `human`, `computer_use`, `retry` and `fallback`; `tirkit.retry`, `tirkit.with_retry` and `tirkit.fallback` record attempts and the option that answered; `Span.mark_first_token()`.
- Trace sampling: `tirkit.init(sample_rate=..., sampling_rules=[...])`, `TIRKIT_SAMPLE_RATE`; whole traces kept or dropped, failed spans always kept.
- `reasoning_tokens` usage unit (OpenAI, Gemini, OTLP); dashboard metrics report prompt-cache hit rate, reasoning tokens and time to first token p50/p95.
- First-run checklist on the Overview: create a key, copy a Python, TypeScript or OpenTelemetry snippet with the project's host and public key, watch "listening" turn into "Got it", then build a dataset, run an experiment, label 20 items and check judge reliability. Each step shows done or not done from real data; the list can be dismissed and disappears when everything is done.
- Empty Traces, Datasets, Experiments, Annotation and Online evals screens offer a one-click path or a ready-to-copy snippet.
- A release workflow (`.github/workflows/release.yml`) and `docs/RELEASING.md`: a version tag builds the wheel with the UI, checks it, and publishes to PyPI (trusted publishing), npm and GHCR.
- CI builds the Docker image and runs `docker compose up` end to end (`.github/workflows/docker.yml`, `scripts/docker_smoke.py`): the UI loads, the first admin is created, a trace sent with the SDK arrives and `/metrics` answers.

### Fixed
- Alerts created through the API without `window_s` showed "NaN min" in the UI.
- Several server or worker processes starting together on a fresh Postgres database could fail while creating the schema; migrations now hold an advisory lock.
- Fork-safe exporter, SQLite store and HTTP client: processes forked by Gunicorn, Celery or `multiprocessing` keep exporting their own spans, without deadlocks.
- `GET /api/projects/{id}/traces/{trace_id}/spans/{span_id}` returns one span in full.
- Tests that re-sending a batch (JSON, OTLP, reversed or split) leaves span and trace counts unchanged.
- Vitest for the web UI (`npm test` in `web/`).
- `research/benchmarks/bench_query.py` runs on SQLite or Postgres at any size (10M spans supported), seeds a 10,000-span trace and times trace opens.
- The wheel now carries the web UI (`make dist` builds it into the package; CI checks the wheel with `scripts/check_wheel.py`), so `pip install "tirkit[server]" && tirkit serve` shows the UI instead of only the API.
- `tirkit backup` on Postgres refuses a `pg_dump` newer than the server, with a message naming both versions: such a dump can contain settings an older server does not know and would fail to restore. CI now installs the Postgres 16 client tools for the server job, and `pip-audit` skips Tirkit's own package.

### Security
- Sign-in throttling per account, per address and server-wide with a progressive delay (never a permanent lock-out), generic errors and `Retry-After`; `TIRKIT_TRUSTED_PROXIES` for the real client address behind reverse proxies.
- Per-API-key ingest limits (default 5,000 spans/s) answered with `429` and `Retry-After`; the Python and TypeScript SDKs wait and resend instead of dropping. Limits are shared across server processes through the database.
- Rate limiter module with token bucket, sliding window and progressive delay on in-process and database backends.
- Security headers on every response (CSP without inline script, `nosniff`, `Referrer-Policy`, frame-ancestors none, HSTS on HTTPS); CORS is same-origin unless `TIRKIT_CORS_ORIGINS` allows origins for the ingestion routes.
- SSRF guard for alert webhooks: public addresses only (including IPv6 and numeric-host tricks), DNS pinned, no redirects, timeout and size cap; `TIRKIT_ALLOW_PRIVATE_WEBHOOKS=1` to opt out.
- Append-only audit log (sign-ins, keys, members, settings, exports, deletes) with an admin API and an **Audit log** page.
- API key rotation with an overlap window, optional expiry and last-used in Settings.
- CI: `pip-audit` and `npm audit --omit=dev` (weekly and on dependency changes) and Dependabot for pip, npm, Actions and Docker. `react-router-dom` 6.28.0 → 7.18.5 for published advisories.
- LLM judge prompt-injection hardening: per-call random delimiters, treat-as-data rules, strict reply format, `suspect_injection` flag; a 78-attack suite and a script that measures attack success against a real model.
- PII redaction modes `off` / `secrets` / `strict` per project (`secrets` for new projects; existing projects keep their behaviour), SDK-side redaction in the exporter thread (`tirkit.init(redact=...)`), a redaction counter in Settings, and a stricter phone-number pattern that no longer mangles long numeric IDs.
- `docs/site/security.md`: threat model, defaults, limits, encryption at rest and reverse-proxy settings.
