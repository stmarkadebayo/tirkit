# Tirkit: build plan

Build in order. Do not start a milestone until the previous one's "Done when" is true. Acceptance
tests in `tests/test_acceptance.py` are the progress meter (`make progress`). At the start of each
milestone, write its acceptance tests from SPEC.md first; they fail until the milestone is built.

Milestones M1–M3 build the core library, which must work on its own (local SQLite, CLI). M4–M5 make it a
product people can run; M6 puts it in front of a real user, whose feedback shapes M7 onward.

## Status (2026-11-16)

| Milestone | State | Notes |
| --- | --- | --- |
| M0 Setup | done | CI workflow covers lint, types, unit/server/Postgres tests, web build, secret scan |
| M1 Core tracing | done | Overhead measured (README Results) |
| M2 Datasets and scorers | done | Includes speech scorers (WER/CER, TTS round trip) |
| M3 Runs, statistics, gate | done | |
| M4 Server, API, deployment | done | Ingestion and query benchmarks in README Results; compose deployment verified with SDK and OTel traffic |
| M5 Web UI, observability | done | Playwright suite not written (DECISIONS #16) |
| M6 First outside user | **maintainer** | Needs a real outside app sending traces for two weeks |
| M7 Web UI, evaluation | done | |
| M8 Prompts and playground | done | Verified against a local Qwen 3.5 9B via LM Studio |
| M9 Online evals, judge reliability | tooling done | Flip rate measured before/after calibration (README Results); E1 kappa needs the maintainer's 100+ labels (docs/E1.md) |
| M10 Ecosystem, gate in the wild | code done | TS SDK, LangChain, transformers, OTLP export, E2 suite; running the gate in an outside repo needs that repo |
| M11 Docs and launch | docs done | Outside users, E3, write-up, video and package releases need the maintainer |

## M0: Setup
- `make setup`, `make test` (smoke tests pass), CI green. `git init`, first commit.
- `.env` from `.env.example`; confirm an OpenAI-compatible endpoint works with a one-line script.
- **Done when:** smoke tests pass locally and in CI.

## M1: Core tracing and local storage
- Span and trace model (§3.1 FR-1, FR-2), `span()`, `@trace`, context propagation (threads, asyncio).
- Background batch exporter; `SQLiteStore` with the §4 schema and indexes; query API (FR-7 subset).
- Media store and generalised usage (tokens, audio seconds, characters, images) with a price table
  covering every unit (FR-4, FR-30, FR-31).
- Auto-instrumentation for the OpenAI client (chat, responses, embeddings, images, speech-to-text,
  text-to-speech; any OpenAI-compatible server, including local small models) and the Anthropic
  client (text and vision), including streaming (FR-3, FR-29).
- Benchmark SDK overhead (NFR-1).
- **Done when:** `test_span_round_trip` passes; nested spans across asyncio tasks keep their parents;
  instrumented OpenAI and Anthropic calls record tokens and cost; overhead in README Results.

## M2: Datasets and scorers
- `Example`, `Dataset`, JSONL/CSV IO, `from_traces` with dedupe (§6.6) and redaction.
- `ExactMatch`, `RuleScorer`, custom function scorers, embedding similarity, WER/CER (FR-33).
- `LLMJudge` (pointwise; pairwise with swap), versioned rubrics, judge cache, budget, built-in templates.
- RAG demo app in `examples/` producing multi-span traces.
- **Done when:** `test_dataset_version_is_content_hash` passes; a dataset is built from demo traces
  with PII redacted (fixture test); judge parse failures are recorded, not scored.

## M3: Experiments, statistics and the gate
- `stats.py` per SPEC §6.1–6.4 plus Spearman.
- `run` (concurrency, retries), `compare`, `gate`; CLI commands; Markdown/HTML reports.
- **Done when:** `test_cohen_kappa_known_value`, `test_bootstrap_ci_contains_mean` and
  `test_gate_blocks_regression` pass; `tirkit compare` prints CIs; `tirkit gate` exits non-zero on a
  seeded regression.

## M4: Server, API and deployment
- FastAPI server (FR-22): batch ingestion endpoint, OTLP/HTTP ingest with GenAI conventions (FR-6),
  query, datasets, experiments and scores APIs; OpenAPI published.
- Accounts (FR-23): users, roles, projects, API keys; project isolation in every query.
- Postgres store and migrations; SQLite single-process `tirkit serve`; `docker compose` (FR-24).
- SDK HTTP exporter; `tirkit.feedback()` (FR-11); ingest redaction (FR-8).
- Benchmarks: ingestion throughput (NFR-5), trace query p95 at 1M spans (NFR-6).
- **Done when:** the demo app sends traces through the SDK and through an OTel exporter to a compose
  deployment; cross-project access tests pass; NFR-5 and NFR-6 measured and in README Results.

## M5: Web UI, observability
- React app scaffold, auth, project switcher, settings (keys, members, model prices).
- Traces list with filters and search; trace tree/waterfall view with rendered messages, documents,
  tool calls and scores; sessions and users views (FR-25).
- Dashboards (FR-9); alerts with webhook and email (FR-10); export and retention (FR-26).
- **Done when:** a new user follows the README from `docker compose up` to seeing their first trace
  in the UI in under 10 minutes (timed, with someone who hasn't seen the project if possible).

## M6: First outside user
- Onboard at least one outside user (friend's app, hackathon team or client) on a self-hosted or
  demo instance, sending real traces and end-user feedback.
- Weekly check-in with them; log their bugs and requests as issues; let them reorder M7–M10.
- **Done when:** an outside app has sent traces every day for two weeks, and their top three requests
  are triaged into the plan.

## M7: Web UI, evaluation
- Datasets: create from filtered traces, upload, edit, versions (FR-12).
- Experiments list; compare view with CIs and per-item diffs (FR-15).
- Score configs and annotation queues with keyboard-driven labelling (FR-18).
- PR-comment report for the CI workflow (FR-16).
- **Done when:** the full loop runs in the UI: trace → dataset → experiment → compare with CIs, and a
  reviewer can label 20 items from a queue.

## M8: Prompts and playground
- Prompt versions and labels, SDK fetch with caching, trace ↔ prompt-version links (FR-20).
- Playground: run a prompt version against a model, replay a traced span, save as a new version (FR-21).
- Experiments started from the UI against a prompt version (FR-14), run by the worker.
- **Done when:** changing the `production` label in the UI changes what the demo app uses without a
  redeploy, and a UI-started experiment appears in the compare view.

## M9: Online evals and judge reliability (E1)
- Worker job queue; evaluator rules with deterministic sampling (§6.7) and budgets (FR-17).
- Maintainer labels 100+ outputs in an annotation queue.
- `tirkit judge-report` and the judge-reliability page (FR-19); calibrate the rubric; keep every version.
- **Done when:** online evals score sampled demo traffic within budget; κ and flip rate before/after
  calibration are in README Results with the method.

## M10: Ecosystem and the gate in the wild (E2)
- `examples/ci-eval.yml` working in another repo with PR comments.
- Seeded-regression suite (E2), 5 seeds.
- TypeScript SDK (FR-27); LangChain and LlamaIndex handlers; OTLP export.
- **Done when:** the gate blocks a real PR with a seeded regression in another repo; E2 numbers in
  README; the TS SDK traces a Node demo into the server.

## M11: Real usage, docs and launch
- Docs site (FR-28): quickstart, concepts, SDK and API reference, self-hosting, integrations.
- Grow to more outside users; run E3 on the first user's traffic (online evals plus end-user
  feedback, started in M6 and M9).
- Public write-up; 60-second demo video; tagged release on PyPI and npm.
- **Done when:** README shows real traces/day and E3, the write-up is published, and every Results
  cell is filled with a measured number.
