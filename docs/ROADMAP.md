# Roadmap

Planned work that is not built yet. [PLAN.md](PLAN.md) stays the source of truth for milestones; items here become milestones when they are scheduled.

**Standing rules for everything below**
- Tirkit runs inside other people's apps. Tracing must add no meaningful latency (current p99 overhead is in README Results; re-measure after any change to the hot path) and ideally help (batching, backoff, sampling, caching).
- Positioning: the differentiator is test sets mined from production and a judge you can trust. The UI leads with judge reliability, not the trace list.
- Two front doors, one product: everything works from the terminal and from the UI, for experienced and new developers alike, and setup works when a coding assistant does it from the docs alone.

## Setup, terminal and coding assistants
- `tirkit init`: detects the project (Python or TypeScript; OpenAI, Anthropic, LangChain and others), writes the `TIRKIT_*` lines to `.env`, adds the two setup lines, sends a test trace and prints its link. Interactive by default, fully scriptable with flags so an assistant can run it.
- `tirkit login`: opens the browser, creates an API key and stores it in the user's config (outside the repo), so nobody pastes keys by hand.
- `tirkit doctor`: checks server, keys, instrumentation and flushing, and says in plain words what to fix.
- Docs written for coding assistants: `llms.txt` and `llms-full.txt` on the docs site, a copy-paste setup prompt in the quickstart and onboarding ("Set up Tirkit in this project using https://…/llms.txt"), and a drop-in skill / `AGENTS.md` section for users' repos.
- MCP server (`tirkit mcp`): assistants can search traces, pull failing cases, build datasets, start experiments and read comparisons and judge reports from the IDE.
- Parity: every UI action has a CLI and API equivalent, every CLI command has `--json`, and each UI screen shows its CLI equivalent.
- No-code path after setup: build a dataset from traces, write a rubric in plain language, run an experiment and read the result in plain words, all from the UI.
- Measure: minutes and steps from nothing to a first trace, by a person following the quickstart and by a coding assistant given only the docs URL.

## Statistical integrity
- An interval on every compared number: weighted and pass/fail kappa, flip rate (stored, not only computed in the UI), length bias, dashboard score trends and score alerts.
- A contract test that fails if any score, rate or agreement field in the API has no interval or sample size.
- Statistics tested against reference implementations, and coverage simulations showing each 95% interval covers the truth about 95% of the time.
- The CI gate's false-alarm and catch rates, measured by simulation.
- A public methodology page: every statistic, its assumptions, minimum sample sizes and what Tirkit does not claim.

## Inference quality
- Inference attributes on spans: engine and version, quantisation, GPU type, time to first token, tokens per second, queue time, batch size (vLLM and SGLang first).
- Config experiments: the same production-mined dataset across serving configs (model, quantisation, engine version, flags), with the quality difference and its 95% CI next to latency and cost per 1,000 requests.
- A cost/quality frontier chart, and a CI gate for model and engine upgrades as well as prompt changes.
- Shadow comparison: score a sampled share of production traffic on a candidate endpoint (budgeted, off by default).
- A measured demo: the same local model at 4-bit and 8-bit on the QA set, quality with a 95% CI, plus speed.

## Security hardening (first)
- Login rate limiting and lockout; per-key ingestion rate limits.
- Security headers on the UI (CSP, frame options, HSTS) and explicit CORS policy.
- SSRF protection on alert webhooks (block private, loopback and metadata addresses).
- Dependency scanning in CI (pip-audit, npm audit, Dependabot).
- Audit log; API-key rotation and expiry; documented encryption-at-rest guidance.
- Judge prompt-injection: test that outputs containing "ignore the rubric" do not move scores, and mitigate (delimiting, instruction hierarchy, injection canaries).

## Providers and multimodal
- Capability layer in `providers.py`: chat, image generation, TTS, ASR, embeddings, video. Playground model-type selector; outputs to the media store; per-image and per-audio-second cost.
- Judge: audio and video attachments (Gemini, GPT audio); κ measured per modality.
- Objective audio scorers: duration, silence, latency, speaker similarity, MOS predictor.
- Annotation UI plays audio and video (check what exists first).
- Local-model wrappers: `@tirkit.model_call(kind=...)` capturing PIL images, numpy audio, bytes; instrumentation for ollama, diffusers, whisper, faster-whisper, LiteLLM. OpenAI-SDK calls to local servers should already trace; verify.

## Agent and trajectory evals
- Trajectory scorers: tool choice, argument validity, order, redundant or looping calls, step and cost budgets, task completion, recovery after tool errors.
- Multi-turn and session-level scoring; trajectory judge with a reliability measurement.
- Datasets of trajectories mined from traces; agent graph view.

## Test generation
- Synthetic cases to widen thin datasets; adversarial and red-team generation; coverage of failure modes.

## Operations
- Backups and restore; Helm chart; Prometheus metrics for Tirkit itself.

## More tracing
- Auto-instrumentation: Bedrock, Cohere, Mistral, Vertex, LiteLLM.
- Integrations: LlamaIndex, CrewAI, Vercel AI SDK, DSPy, OpenAI Agents SDK.
- Span kinds: vector DB, reranker, MCP tool call, human-in-the-loop, computer-use with screenshots.
- Time-to-first-token as a first-class metric; retry and fallback spans; cache hit rate; reasoning-token spans; sampling rules.

## Visualisation
- Service/dependency map, agent graph, latency heatmap, cost breakdown (model, user, release, prompt version), trace diff, failure clustering, session replay, judge-vs-human disagreement browser, eval run heatmap, token waterfall, live tail.
- UI rebuild approach and stack: to decide (design pass first; launch and look at the current UI before adding charts).

## Bar-defining work (do not let features crowd it out)
- Outside users sending traces (M6), judge–human κ (needs 100+ labels), seeded regressions caught in CI, E3.
