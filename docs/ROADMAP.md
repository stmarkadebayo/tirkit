# Roadmap

Planned work that is not built yet. [PLAN.md](PLAN.md) stays the source of truth for milestones; items here become milestones when they are scheduled.

**Standing rules for everything below**
- Tirkit runs inside other people's apps. Tracing must add no meaningful latency (current p99 overhead is in README Results; re-measure after any change to the hot path) and ideally help (batching, backoff, sampling, caching).
- Positioning: the differentiator is test sets mined from production and a judge you can trust. The UI leads with judge reliability, not the trace list.

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
