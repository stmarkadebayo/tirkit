# Changelog

All notable changes to Tirkit are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Tirkit uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Code is organised in enforced layers (core, storage, SDK, evaluation, services, delivery); `make lint` runs the import-linter contracts. Shared types live in `tirkit.types` and are still importable from their old modules.
- `tirkit.store` is now a package and the server is split into per-domain routers; public imports are unchanged.
- Request validation errors return 400 with `{"detail": "..."}` like every other error.
- The web UI's API types are generated from the server's OpenAPI schema (`make api`).
- Repository layout: `SPEC.md` and `PLAN.md` moved to `docs/`; contributor and policy files (`CONTRIBUTING.md`, `CLA.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`) moved to `.github/`; `benchmarks/`, `experiments/` and `evals/` moved under `research/`; tool caches and the built docs site go to `.cache/`.
- `GET /api/projects/{id}/traces/{trace_id}` now returns a light span tree (ids, parents, timings, tokens, cost, short previews, paged with an explicit `omitted` count); use `?full=1` for the previous shape or `.../spans/{span_id}` for one full span.
- Ingestion decodes, validates and writes off the event loop, so `/api/health` stays fast while a slow batch is written.
- SDK retries honour `Retry-After`, use jittered exponential backoff and stop after 30 s (Python and TypeScript); the Python exporter defaults to 5 retries instead of 3.
- The trace page draws only the rows in view and shows every span of a trace, including spans in parent cycles, self-parents and orphans (previously these disappeared); the span tree is built without quadratic copying.

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

### Fixed
- Alerts created through the API without `window_s` showed "NaN min" in the UI.
- Fork-safe exporter, SQLite store and HTTP client: processes forked by Gunicorn, Celery or `multiprocessing` keep exporting their own spans, without deadlocks.
- `GET /api/projects/{id}/traces/{trace_id}/spans/{span_id}` returns one span in full.
- Tests that re-sending a batch (JSON, OTLP, reversed or split) leaves span and trace counts unchanged.
- Vitest for the web UI (`npm test` in `web/`).
- `research/benchmarks/bench_query.py` runs on SQLite or Postgres at any size (10M spans supported), seeds a 10,000-span trace and times trace opens.
