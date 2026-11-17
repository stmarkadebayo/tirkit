# Python SDK

```python
import tirkit

tirkit.init()  # server mode with TIRKIT_HOST, else local tirkit.db
tirkit.instrument()  # "openai", "anthropic", "transformers"
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
| `tirkit.flush()` | Write queued spans now (also done at exit) |
| `tirkit.register_price("my-model", input_tokens=2e-6, output_tokens=8e-6)` | Price a model |

## Delivery

Spans are queued and sent by a background thread, so a traced call never waits for the network or
the database. If the queue (10,000 spans) is full, spans are dropped and counted
(`tirkit.get_client().exporter.stats()`), never blocking your code.

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
