# Python SDK

```python
import tirkit

tirkit.init()  # server mode with TIRKIT_HOST, else local tirkit.db
tirkit.instrument()  # every installed SDK; or tirkit.instrument("openai", "bedrock", ...)
```

| Call | What it does |
| --- | --- |
| `tirkit.span(name, kind=..., input=..., **attributes)` | Context manager; `s.output = ...`, `s.set_usage(input_tokens=...)` |
| `@tirkit.trace` / `@tirkit.trace(kind="tool")` | Trace every call of a function (sync, async, generators) |
| `tirkit.trace_context(session_id=..., user_id=..., release=..., tags=[...])` | Trace-level fields for spans inside |
| `tirkit.Attachment(data=png_bytes, mime="image/png")` | Explicit media in an input or output |
| `tirkit.feedback(trace_id, 1, comment="...")` | End-user feedback as a score |
| `tirkit.get_prompt("name", label="production")` | Fetch a prompt; `.compile(**vars)`; `with prompt.use():` links spans |
| `tirkit.copy_context_run(fn)` | Keep the parent span when submitting work to a thread pool |
| `@tirkit.retry(attempts=3, retry_on=(RateLimitError,), backoff=0.5)` / `tirkit.with_retry(fn, ...)` | Retry, with one span per attempt under a `retry` span |
| `tirkit.fallback(primary, secondary, name="answer")` | Try options in order; a `fallback` span records which one answered |
| `tirkit.init(sample_rate=0.1, sampling_rules=[...])` / `tirkit.set_sampling(...)` | Keep a fraction of traces (see below) |
| `span.mark_first_token()` | Record time to first token for a stream you handle yourself |
| `tirkit.flush()` | Write queued spans now (also done at exit) |
| `tirkit.init(redact="strict")` | Redact PII before spans leave your process (`"off"`, `"secrets"`, `"strict"`; also `TIRKIT_REDACT`). Runs in the background exporter thread; see [Security](security.md) |
| `tirkit.register_price("my-model", input_tokens=2e-6, output_tokens=8e-6)` | Price a model |

## Delivery

Spans are queued and sent by a background thread, so a traced call never waits for the network or
the database. If the queue (10,000 spans) is full, spans are dropped and counted
(`tirkit.get_client().exporter.stats()`), never blocking your code.

**If the server is down.** Tracing fails open: `tirkit.init()` makes no network call, traced code never waits for the network, and an unreachable, erroring or hanging server never raises into your app. Lost spans are counted in `stats()` and one warning is logged per outage (not one per batch). A batch is retried with backoff for up to 30 seconds (the server's `Retry-After` wins); when your app exits it waits at most 5 seconds to send what is queued. `tests/test_fail_open.py` checks all of this.

- **Retries.** A failed batch is retried (timeouts, connection errors, 408, 429 and 5xx) with
  jittered exponential backoff (up to 5 s) and at most 5 retries within 30 s
  (`max_retries`, `max_retry_s` on `BatchExporter`). A `Retry-After` header from the server is
  honoured instead of the backoff, unless it would exceed the 30 s budget. Other 4xx errors fail at
  once and are counted as failed. Re-sending a batch is safe: ingestion is idempotent.
- **Forking.** Gunicorn, Celery and `multiprocessing` (fork start method) work: a forked process gets
  its own exporter thread, queue, HTTP connections and SQLite connection, and exits with its spans
  flushed. Spans queued in the parent before the fork are exported by the parent.

## What instrumentation records

- **OpenAI** (and any OpenAI-compatible server: Ollama, vLLM, LM Studio, OpenRouter, ...): chat
  completions (vision input, audio in and out, tools, streaming), Responses API, embeddings, image
  generation and edits, speech-to-text (transcriptions, translations, streaming) with audio duration
  and real-time factor, text-to-speech with the audio.
- **Anthropic:** `messages.create` (incl. `stream=True`) and `messages.stream()`, vision and
  document inputs, tool use, thinking blocks, prompt-cache reads and writes.
- **transformers:** every `pipeline(...)` call, with the span kind chosen from the task.

## Span kinds

`llm`, `embedding`, `rerank`, `retrieval`, `tool`, `agent`, `chain`, `guardrail`, `evaluator`,
`image_generation`, `speech_to_text`, `text_to_speech`, `speech_to_speech`, `generic`, and
`vector_db` (a vector store query), `mcp_tool` (a Model Context Protocol tool call), `human` (waiting
for a person), `computer_use` (a computer-use action), `retry` and `fallback`.

```python
with tirkit.span("search-docs", kind="vector_db", input=query, db="pgvector") as s: ...
with tirkit.span("approve-refund", kind="human") as s: ...
```

## Sampling

Tracing every request can be too much at high volume. Sampling is decided once per trace, at the
root span, and inherited by every child, so a trace is kept whole or dropped whole; the decision
hashes the trace id, so every process agrees.

```python
tirkit.init(sample_rate=0.1)                       # or TIRKIT_SAMPLE_RATE=0.1
tirkit.init(sample_rate=0.05, sampling_rules=[
    {"name": "health*", "rate": 0},                # by root span name (glob)
    {"kind": "agent", "rate": 1.0},                # or by kind; first match wins
])
```

A span that fails is kept even when its trace was not sampled, together with the spans of that
trace that finish after it (its parents), so errors are never lost. Pass
`tirkit.set_sampling(rate, rules, keep_errors=False)` to drop them too. Spans that arrive over
OTLP are not sampled by Tirkit: sample in your exporter or Collector.

## Dashboard metrics

The metrics endpoint reports, per time bucket and optional group (model, name, release, user,
kind): the **prompt-cache hit rate** (cached input tokens / input tokens), **reasoning tokens**,
and **time to first token** p50 and p95, next to tokens, cost and latency percentiles. Reasoning
tokens are part of output tokens and are billed as output.

## Evaluation API

```python
from tirkit.datasets import Dataset, Example, from_traces
from tirkit.runner import run, compare, gate
from tirkit.scorers import ExactMatch, RuleScorer, rules, WordErrorRate, TTSRoundTrip
from tirkit.judge import LLMJudge, OpenAICompatibleJudge, AnthropicJudge

ds = Dataset.from_jsonl("evals/questions.jsonl")
judge = LLMJudge(OpenAICompatibleJudge("gpt-4o-mini"), "correctness")
base = run(ds, answer_v1, [ExactMatch(normalise=True), judge], store=tirkit.get_client().store)
cand = run(ds, answer_v2, [ExactMatch(normalise=True), judge], store=tirkit.get_client().store)
for c in compare(base, cand):
    print(c.scorer, c.diff, c.diff_ci, c.verdict)
assert gate(base, cand, "judge:correctness", max_drop=0.1)
```
