# Tirkit: build specification

## 1. Summary
Tirkit is a self-hostable, open-source platform for **observing** and **evaluating** LLM applications.
It is meant to stand as a product of its own, at feature level with the established tools in this
category (LangSmith, Langfuse, Arize Phoenix, Braintrust, W&B Weave), and to go beyond them in two
places: it measures whether its own LLM judge can be trusted, and it reports every comparison with
proper statistics.

It covers the full loop:

1. **Observe:** trace every LLM call, tool call, retrieval and agent step from production. Track
   cost, latency, errors, sessions, users and releases. Explore it in a web UI with dashboards.
2. **Collect feedback:** capture end-user feedback, human annotations and automatic scores on traces.
3. **Build datasets:** turn real traces into versioned, PII-redacted test sets.
4. **Experiment:** manage prompts as versioned assets, try them in a playground, run experiments
   over datasets and compare them side by side with confidence intervals.
5. **Evaluate continuously:** score sampled production traffic automatically (online evals), and
   gate pull requests on offline evals in CI.
6. **Trust the judge:** measure judge–human agreement, position bias and length bias, and calibrate.

## 2. Goals and non-goals
**Goals**
- A product a team could adopt today: one-command self-hosting, a web UI, an API, SDKs and docs.
- Add tracing to an app in a few lines, or with zero code changes through auto-instrumentation and
  OpenTelemetry.
- Cover both halves fully: **observability** (traces, sessions, cost, latency, dashboards, alerts)
  and **evaluation** (datasets, experiments, scorers, annotation, online evals, CI gate).
- Statistically honest comparisons everywhere: confidence intervals, never a lone mean.
- A judge-reliability report: agreement with human labels, position bias and length bias.

**Non-goals (for this project)**
- A multi-tenant hosted SaaS, billing, or usage metering for customers.
- SSO/SAML and fine-grained enterprise RBAC (basic roles only; SSO is a later extension).
- An LLM gateway or router (Tirkit works with any OpenAI-compatible gateway).
- Columnar analytics stores (ClickHouse) before Postgres measurably stops being enough (see §7).
- Model training or fine-tuning.

## 3. Requirements

### 3.1 Tracing and observability
- FR-1 **Span API.** `tirkit.span()` context manager and `@tirkit.trace` decorator record: name, type
  (`llm`, `chat`, `embedding`, `retrieval`, `tool`, `agent`, `chain`, `generic`), trace id, span id,
  parent id, start, end, status, model, model parameters, input (messages or arbitrary JSON), output,
  input and output tokens, cost in USD, error, level, and free-form attributes. Nesting is automatic
  through context variables (threads and asyncio both supported).
- FR-2 **Trace-level fields.** A trace carries name, `session_id`, `user_id`, `release`, `version`,
  tags, metadata, and its root input and output. Sessions group traces into a conversation or thread.
- FR-3 **Auto-instrumentation.** One call (`tirkit.instrument()`) traces the OpenAI Python client
  (and any OpenAI-compatible endpoint through it) and the Anthropic Python client, including streaming,
  tool calls, tokens and cost. LangChain and LlamaIndex callback handlers follow in a later milestone.
- FR-4 **Cost.** Cost is computed from a model price table (input, output and cached-token prices,
  editable per project in the UI) when the caller does not supply it.
- FR-5 **Transport.** The SDK batches spans on a background thread and sends them either to a local
  SQLite file (offline mode) or to a Tirkit server over HTTP. The SDK never raises into user code and
  never blocks the hot path; when it must drop data, it counts drops and logs a warning.
- FR-6 **OpenTelemetry.** The server accepts OTLP/HTTP traces (protobuf and JSON) and maps the
  OpenTelemetry GenAI semantic conventions (and OpenInference attributes where practical) onto Tirkit
  spans, so any OTel-instrumented app in any language can send data. Tirkit spans can also be exported
  as OTLP.
- FR-7 **Query.** Spans and traces can be filtered by time range, name, type, model, user, session,
  release, tag, metadata equality, score value, error status, cost and latency, with full-text search
  over inputs and outputs. Available in the API, SDK and UI.
- FR-8 **Redaction at ingest.** Configurable masking (emails, phone numbers, card-like numbers, API
  keys, custom regexes) can run before data is stored, per project.
- FR-9 **Dashboards.** Volume, cost, tokens, latency p50/p95/p99, error rate and score averages over
  time, broken down by model, release, user, trace name and tag.
- FR-10 **Alerts.** Threshold rules on error rate, cost, latency or a score average over a window,
  delivered by webhook (Slack-compatible) and email.
- FR-11 **End-user feedback.** An API and SDK call (`tirkit.feedback(trace_id, value, comment)`) records
  thumbs, ratings or free text from an app's users as scores on the trace.

### 3.2 Evaluation
- FR-12 **Datasets.** Create datasets from traces (filter in the UI or `tirkit dataset build
  --from-traces`), upload JSONL or CSV, add and edit items, and download them. Building from traces
  deduplicates exact and near-duplicate inputs (§6.6) and redacts PII before saving. Every change
  produces a new version identified by a content hash; old versions stay readable.
- FR-13 **Scorers.** One scorer protocol, many kinds:
  - `ExactMatch`, `RuleScorer` (regex, JSON-schema validity, max length, banned phrases, contains);
  - custom Python functions;
  - `LLMJudge`, pointwise (1–5 score with a reason) and pairwise (a/b/tie with order swap, §6.5);
  - built-in judge templates: correctness against a reference, faithfulness/hallucination against
    context, answer relevance, context relevance (RAG), toxicity, conciseness;
  - embedding similarity against a reference.
  Every judge prompt is a versioned rubric. Judge calls are cached by (rubric version, model, input,
  output) hash and respect a USD budget.
- FR-14 **Experiments.** `tirkit.run()` / `tirkit run` executes a target over a dataset version with
  configurable concurrency and retries, scores every output with every scorer, traces each target
  call, and stores outputs, scores, latency and cost. Experiments can also be started from the UI
  against a prompt version and model (no code).
- FR-15 **Comparison.** `tirkit compare A B` and the UI compare view show, per scorer, each run's mean
  with a 95% CI and the paired difference with its 95% bootstrap CI (§6.1–6.2), plus a per-item
  side-by-side diff of outputs and scores, filterable to regressions and improvements.
- FR-16 **CI gate.** `tirkit gate` exits non-zero only if the candidate is confidently worse than the
  baseline by more than `--max-drop` (§6.3). A ready-to-copy GitHub Actions workflow
  (`examples/ci-eval.yml`) runs it on pull requests and posts a Markdown report as a PR comment.
- FR-17 **Online evals.** Evaluator rules attach scorers to production traffic: a trace filter, a
  sampling rate and a scorer. A background worker scores matching traces as they arrive, within the
  project's judge budget. Results appear as scores on traces and in dashboards.
- FR-18 **Human annotation.** Score configs define what can be scored (numeric range, categories,
  boolean). Annotation queues hold traces or dataset items for review; the UI shows one item at a
  time with keyboard shortcuts so each label takes seconds. A terminal fallback (`tirkit label`) exists.
- FR-19 **Judge reliability.** `tirkit judge-report` and a UI page compare judge scores with human
  labels and report Cohen's kappa, the position-swap flip rate and the length–score correlation,
  per rubric version, so calibration progress is visible (§6.4–6.5).

### 3.3 Prompts and playground
- FR-20 **Prompt management.** Prompts (text or chat templates with variables) are versioned. Labels
  such as `production` and `staging` point at versions. The SDK fetches a prompt by name and label,
  caches it, and links each trace to the prompt version that produced it.
- FR-21 **Playground.** In the UI, fill a prompt's variables, pick a model and parameters, and run it
  against any OpenAI-compatible endpoint; open any traced LLM span in the playground to replay and
  edit it; save the result as a new prompt version.

### 3.4 Platform
- FR-22 **Server.** A FastAPI server with a versioned REST API (OpenAPI schema published) for
  ingestion, query, datasets, experiments, scores, prompts, annotation and admin.
- FR-23 **Accounts.** A single organisation per deployment with users (email and password), roles
  (admin, member, viewer), projects, and per-project API keys (public key plus secret key; secrets
  stored hashed).
- FR-24 **Deployment.** `docker compose up` starts the server, worker and Postgres. `pip install
  "tirkit[server]" && tirkit serve` runs everything in one process on SQLite for local use.
- FR-25 **Web UI.** Traces (list, tree/waterfall view, rendered chat messages and retrieved
  documents), sessions, users, dashboards, datasets, experiments and compare, prompts and playground,
  annotation queues, evaluators, judge reliability, settings (projects, keys, prices, members).
- FR-26 **Data export and retention.** Export traces, scores and datasets as JSONL/CSV via the API
  and UI. Per-project retention period with automatic deletion.
- FR-27 **TypeScript SDK.** Tracing, feedback, prompt fetching and OpenAI auto-instrumentation for
  Node.js, matching the Python SDK's concepts.
- FR-28 **Docs.** A docs site with quickstart, concepts, SDK and API reference, self-hosting guide
  and integration guides.

### 3.5 Every kind of model
Tirkit traces and evaluates any model, not only text chat models.
- FR-29 **Model kinds.** Span kinds cover text generation and chat, vision-language (image input),
  image generation, embeddings, reranking, speech-to-text (ASR/STT), text-to-speech (TTS),
  speech-to-speech (realtime voice), and small or local models (any OpenAI-compatible server such as
  Ollama, vLLM or LM Studio, and in-process Hugging Face pipelines).
- FR-30 **Media.** Span inputs and outputs can hold images and audio. Media is stored once by SHA-256
  in a media store (a local directory, or the server's blob store) and referenced from the span JSON
  as `{"type": "media", "sha256", "mime", "size"}`. Base64 data inside provider payloads (data URIs,
  `input_audio`, TTS output) is extracted automatically so the database never holds large blobs. The UI
  renders images and plays audio.
- FR-31 **Generalised usage and cost.** Usage is a set of metered units: input/output/cached tokens,
  audio seconds in and out, characters, images and requests. The price table prices any of them, so
  cost works for TTS (per character), ASR (per audio minute) and image models (per image) as well as
  token models.
- FR-32 **Modality metrics.** Latency metrics include time to first token or first audio byte, and the
  real-time factor (processing time ÷ audio duration) for speech.
- FR-33 **Modality scorers.** Word error rate and character error rate for ASR; round-trip
  intelligibility for TTS (transcribe the audio with a reference ASR model, then WER against the
  input text); LLM judges that accept images for vision tasks; text scorers for everything else.

### 3.6 Non-functional
- NFR-1 The SDK adds under 1 ms p99 per traced call (writes batched off the hot path).
- NFR-2 No personal data is persisted unredacted in datasets; ingest redaction is available for traces.
- NFR-3 Works offline except for LLM calls. Judge calls respect a USD budget set per project.
- NFR-4 Every number shown in reports is reproducible from stored data with a fixed seed.
- NFR-5 The server ingests at least 1,000 spans/s on a laptop with Postgres, without dropping data.
- NFR-6 Trace list queries return in under 500 ms p95 with 1 million stored spans.
- NFR-7 Security: API keys and passwords hashed (argon2), CSRF protection on the UI, per-project data
  isolation enforced in every query, no secrets in logs. Tests cover cross-project access.
- NFR-8 The SDK's core install has no heavy dependencies; server dependencies are an optional extra.

## 4. Architecture
```
 Your app ── Python SDK / TS SDK / any OTel exporter
              │  (batched HTTP: /api/v1/ingest, /v1/traces OTLP)
              ▼
        Tirkit server (FastAPI) ── REST API ── Web UI (React, served by the server)
              │                     ▲
              ▼                     │
        Postgres (SQLite locally) ◀─┤
              ▲                     │
        Worker: online evals, experiments started from the UI, alerts, retention
```

| Module | Responsibility | Key interface |
| --- | --- | --- |
| `tirkit/tracing.py` | Span and trace model, `span()`, `@trace`, context propagation | `span(name, sink, parent=None, **attrs)` |
| `tirkit/client.py` | SDK client: config, background batch exporter (local or HTTP), feedback, prompt fetch | `Tirkit(public_key, secret_key, host)` |
| `tirkit/instrument/` | Auto-instrumentation for OpenAI, Anthropic; LangChain/LlamaIndex handlers | `tirkit.instrument()` |
| `tirkit/pricing.py` | Model price table and cost calculation | `cost(model, usage)` |
| `tirkit/store.py` | Storage interface; SQLite and Postgres implementations | `Store.save_spans / query_spans / ...` |
| `tirkit/datasets.py` | `Example`, `Dataset` (content-hash version), mining from traces, JSONL/CSV IO | `from_traces(spans, dedupe=True, redact=True)` |
| `tirkit/redact.py` | PII redaction with placeholders | `redact(text) -> str` |
| `tirkit/scorers.py` | Scorer protocol and implementations, judge templates, judge cache | `Scorer.score(example, output) -> Score` |
| `tirkit/stats.py` | Bootstrap CIs, paired bootstrap, Cohen's kappa, Spearman | see §6 |
| `tirkit/runner.py` | Experiments, compare, gate | `run(dataset, target, scorers)`, `compare(a, b)`, `gate(a, b, scorer, max_drop)` |
| `tirkit/report.py` | Markdown/HTML reports (PR comments, static reports) | `render_run`, `render_compare`, `render_judge_report` |
| `tirkit/cli.py` | Typer CLI | `tirkit run / compare / gate / dataset / label / judge-report / serve` |
| `tirkit/server/` | FastAPI app: ingestion, OTLP, REST API, auth, static UI | `create_app(settings)` |
| `tirkit/worker/` | Postgres-backed job queue: online evals, UI experiments, alerts, retention | `tirkit worker` |
| `tirkit/otel.py` | OTLP mapping in and out | `OTelSink`, `from_otlp(...)` |
| `web/` | React + TypeScript UI (Vite), built into static assets | — |
| `sdk-ts/` | TypeScript SDK | `@tirkit/sdk` |

**Storage schema** (Postgres; the same tables in SQLite). Every table except `users` carries
`project_id` and every query filters on it.
- `projects`, `users`, `memberships(user_id, project_id, role)`, `api_keys(public_key, secret_hash, project_id)`
- `traces(trace_id PK, project_id, name, session_id, user_id, release, version, tags, input JSON, output JSON, metadata JSON, start, end)`
- `spans(span_id PK, trace_id, parent_id, project_id, name, type, status, level, start, end, model, model_params JSON, input JSON, output JSON, input_tokens, output_tokens, cost_usd, error, prompt_version_id, attributes JSON)`
- `scores(score_id PK, project_id, trace_id, span_id, experiment_item_id, name, value_num, value_str, source [api|human|rule|llm_judge|user_feedback], scorer_version, reason, author_id, created)`
- `score_configs(name, project_id, data_type, min, max, categories JSON)`
- `datasets(name, project_id, created)`, `dataset_versions(dataset, version, created, n_items)`, `dataset_items(dataset, version, idx, input JSON, expected JSON, metadata JSON, source_trace_id)`
- `experiments(experiment_id PK, project_id, dataset, version, target, prompt_version_id, config JSON, created)`, `experiment_items(experiment_id, idx, trace_id, output JSON, latency_ms, cost_usd, error)`
- `prompts(name, project_id)`, `prompt_versions(id PK, name, version, template JSON, config JSON, created_by)`, `prompt_labels(name, label, version)`
- `rubrics(name, version, text, model, created)`, `judge_cache(hash PK, result JSON)`
- `evaluators(id PK, project_id, filter JSON, sampling_rate, scorer JSON, active)`
- `annotation_queues(id, project_id, name, score_config_names JSON)`, `queue_items(queue_id, trace_id | dataset_item, status, assignee)`
- `model_prices(project_id, model_pattern, input_per_mtok, output_per_mtok, cached_per_mtok)`
- `alerts(id, project_id, rule JSON, channel JSON)`, `jobs(id, kind, payload JSON, status, attempts, run_after)`

Indexes: spans on `(project_id, start)`, `(project_id, trace_id)`, `(project_id, name)`; traces on
`(project_id, start)`, `(project_id, session_id)`, `(project_id, user_id)`; scores on
`(project_id, trace_id)`, `(project_id, name, created)`. Full-text search uses Postgres `tsvector`
(SQLite FTS5 locally).

## 5. Data
- **Development traffic:** a demo app in `examples/` (a RAG question-answering agent over a few
  documents, with a retrieval step and a tool call) generates realistic multi-span traces locally.
- **Human labels for judge calibration:** the maintainer labels 100–200 outputs (pass/fail plus 1–5 score)
  in an annotation queue. Labels are exported to `labels/*.jsonl` for the judge report.
- **Public sets for regression demos:** a small open QA set (for example a slice of SQuAD or a
  hand-written set of 50 questions) is enough; record which in DECISIONS.md.
- **Real traffic:** at least one outside app sends traces (see §10).

## 6. Method details
### 6.1 Bootstrap confidence interval
Percentile bootstrap of the mean: resample with replacement n values, 10,000 times by default, seeded;
return the (α/2, 1−α/2) percentiles of the resampled means.

### 6.2 Paired comparison
For two runs on the same dataset version, compute per-example differences d_i = a_i − b_i and
bootstrap the mean of d. Report mean(d) and its 95% CI. Refuse to compare runs on different dataset
versions. Items that errored in either run are excluded and counted in the report.

### 6.3 Gate rule
Let d = candidate − baseline per example. The gate **fails** if the upper bound of the 95% CI of
mean(d) is below −max_drop (the candidate is confidently worse by more than the allowed drop).
Otherwise it passes. This avoids flaky failures from noise.

### 6.4 Cohen's kappa
κ = (p_o − p_e) / (1 − p_e), where p_o is observed agreement and p_e is chance agreement from the two
raters' marginal distributions. If p_e = 1, return 1.0 when p_o = 1, else 0.0.

### 6.5 Judge design
- Pointwise: rubric prompt, output must be JSON `{"score": 1-5, "reason": "..."}`; parse failures are
  retried once, then recorded as failures (not silently scored).
- Pairwise: present A and B, then repeat with the order swapped. **Flip rate** = share of pairs where
  the preferred answer changes when the order changes (ties excluded from the denominator). Final
  pairwise verdict is a win only if both orders agree; otherwise a tie.
- Length bias: Spearman correlation between output length (tokens) and judge score, compared with the
  same correlation for human scores.
- Calibration: improve the rubric (anchored examples, explicit criteria, swap-averaging) until kappa
  against human labels stops improving; keep every rubric version and its kappa.

### 6.6 Near-duplicate removal
Normalise text (lowercase, collapse whitespace), then drop examples whose word 5-gram Jaccard
similarity to an earlier kept example is ≥ 0.9. Keep the threshold configurable.

### 6.7 Online-eval sampling
An evaluator scores a trace if `hash(trace_id, evaluator_id) mod 10,000 < sampling_rate × 10,000`,
so sampling is deterministic and reproducible. When the project's judge budget for the day is spent,
jobs are skipped and counted, never queued indefinitely.

### 6.8 Dashboard percentiles
Latency percentiles are exact over the selected window up to 100k spans and computed with
`percentile_cont` in Postgres; beyond that, the UI states the sample size used.

## 7. Decisions (made)
| Decision | Choice | Why |
| --- | --- | --- |
| Product shape | Self-hosted open source, single organisation per deployment | Product-grade without SaaS billing or multi-tenancy; fits the free-compute constraint |
| Licence | Apache-2.0 for everything (SDKs, server, UI); contributions under a CLA | Matches OpenTelemetry and most of the category; the CLA keeps a future commercial edition possible |
| Storage | SQLite locally, Postgres for the server | Zero setup locally; Postgres handles the target scale (NFR-5, NFR-6) |
| Analytics store | No ClickHouse until Postgres misses NFR-6 in a measured benchmark | Avoid a second database before it is needed |
| Job queue | Postgres table with `SELECT ... FOR UPDATE SKIP LOCKED` | No Redis to operate |
| Server | FastAPI, optional extra `tirkit[server]` | Typed, async, OpenAPI for free; keeps the SDK light |
| UI | React + TypeScript + Vite, served as static files by the server | Trace trees and compare views need a real client-side app |
| Trace schema | Columns for common fields plus JSON `attributes`/`metadata` | Fast filters without a rigid schema |
| Interop | OTLP ingest with GenAI semantic conventions | Any language, any OTel-instrumented framework |
| Judge | Pointwise for scoring, pairwise with order swap for comparisons | Pairwise is more reliable but needs bias control |
| Gate | CI-based rule in §6.3 | Thresholds on raw means are flaky |
| Dataset versioning | SHA-256 content hash (first 12 hex chars) | Same examples always give the same version |
| LLM access | Any OpenAI-compatible endpoint via `LLM_BASE_URL` | Provider-agnostic; works behind any gateway |

## 8. Evaluation and metrics
- Judge–human agreement (κ) before and after calibration, on the same labelled set.
- Position-swap flip rate, before and after calibration.
- Seeded regressions caught: create N=20 deliberately worse prompt variants (dropped instructions,
  truncated context, wrong format) plus 20 neutral variants; report caught/20 and false alarms/20.
- SDK tracing overhead: p50/p99 added latency over 10,000 traced no-op calls.
- Server ingestion throughput (spans/s sustained without drops) and trace-list query p95 at 1M spans.
- Real usage: traces per day from at least one outside user.

## 9. Experiments
- **E1 (judge bias):** label 100 outputs; run the judge in both orders; measure κ and flip rate;
  calibrate; measure again. Claim tested: LLM judges are biased by position and length, and
  calibration against human labels improves agreement.
- **E2 (gate reliability):** run the seeded-regression suite 5 times with different seeds; report
  catch rate and false-alarm rate per run.
- **E3 (online evals vs users):** on real traffic, measure the correlation between online judge
  scores and end-user feedback on the same traces. Claim tested: offline-style judge scores predict
  what users actually think.

## 10. Deliverables for 9/10
- `docker compose up` gives a working instance; a new user goes from zero to their first trace in the
  UI in under 10 minutes, following the docs.
- Real traffic from at least one outside project traced through Tirkit.
- The full loop demonstrated in the UI: trace → dataset → experiment → compare with CIs → CI gate.
- Online evals running on real traffic; annotation queue used for the human labels.
- E1, E2 and E3 results in the README with method notes.
- The CI gate running in another repository (any outside repository).
- A docs site and a public write-up.

## 11. Risks
- **Scope:** this is a large product. Build in PLAN.md order and keep each milestone shippable; the
  core library (M1–M3) must work on its own before the server and UI.
- **Judge cost:** cap with per-project budgets; cache judge calls by (rubric version, model, input,
  output) hash.
- **Label effort:** 100 labels minimum; the annotation UI must make each label take seconds.
- **Redaction misses:** test redaction on a fixture of known PII strings; never store raw traces in datasets.
- **Security:** a self-hosted server holding prompts and user data; test project isolation and auth
  from the first server milestone.
- **Postgres at scale:** benchmark NFR-6 early (M4) so the storage decision is made on data.

## 12. References
- *AI Engineering*: Evaluation Methodology; Evaluate AI Systems; AI Engineering Architecture and User Feedback.
- OpenTelemetry GenAI semantic conventions and OpenInference (verify current versions before M4).
- The products named in §1, for feature reference only: read their public docs; copy no code or UI assets.
