# `tirkit.instrumentation`: provider auto-instrumentation

Patches provider SDKs (OpenAI, Anthropic, Gemini, OpenAI Realtime) so their calls become spans.
`tirkit.instrument()` applies every adapter whose SDK is installed.

## What belongs here

One module per provider SDK, registered in `__init__.py`'s `_registry()`, built on the helpers in
`_patch.py` (`CallSpec`, `wrap_method`, `patch`). An adapter maps the SDK's request and response
to a `Span`: kind, model, input, output, usage, cost, errors, streaming and async variants.

Framework callbacks (LangChain, transformers pipelines) live in `tirkit.integrations`, not here.

## Rules

- This is the SDK layer. It may import only `tirkit.tracing`, `tirkit.types` and core modules
  (`serialize`, `media`, `pricing`, ...). Never the server, the store, or evaluation code.
- It runs inside other people's apps: no network calls, no heavy work, and never raise into the
  caller. Import the provider SDK lazily and do nothing when it is absent.
- Each adapter needs fake-client tests with no network: streaming, async, errors, usage and cost.
- Re-run `make bench` after touching anything on the call path.

## Run only this area

```bash
.venv/bin/pytest -q tests/test_instrument.py tests/test_gemini.py tests/test_realtime.py
make bench
```
