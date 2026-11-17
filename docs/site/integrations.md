# Integrations

## OpenTelemetry (any language, any framework)

Tirkit accepts OTLP/HTTP traces at `POST /v1/traces` (protobuf or JSON) and maps the OpenTelemetry
GenAI semantic conventions (`gen_ai.*`) and OpenInference attributes onto spans: model, tokens,
cache tokens, messages, tool calls, retrieval, conversation id, user id.

```bash
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:8000/v1/traces
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Basic%20$(printf '%s:%s' "$TIRKIT_PUBLIC_KEY" "$TIRKIT_SECRET_KEY" | base64)"
```

That covers LlamaIndex, CrewAI, Haystack, DSPy, the Vercel AI SDK, Spring AI and OpenLLMetry's
provider instrumentations (Bedrock, Vertex AI, Gemini, Mistral, Cohere, Groq, ...). See
`examples/otel_demo.py`.

Tirkit can also *export* spans to any OTLP collector with `tirkit.otel.OTelSink(endpoint)`.

## LangChain and LangGraph

```python
from tirkit.integrations.langchain import TirkitCallbackHandler

graph.invoke(inputs, config={"callbacks": [TirkitCallbackHandler()]})
```

Chains, chat models (messages, tokens, model), tools and retrievers become nested spans.

## Hugging Face transformers (small and local models, speech, vision)

```python
tirkit.instrument("transformers")
asr = pipeline("automatic-speech-recognition", model="openai/whisper-tiny")
asr(audio)  # span kind speech_to_text, audio stored, audio seconds and real-time factor recorded
```

## Local and small models over HTTP

Point the OpenAI client at Ollama (`http://localhost:11434/v1`), LM Studio (`:1234/v1`), vLLM or
llama.cpp; `tirkit.instrument()` traces them with `attributes.provider = "openai-compatible"`.
Set `LLM_BASE_URL` on the server to use them in the playground, UI experiments and online evals.

## CI

`examples/ci-eval.yml` runs your eval on every pull request, gates on it and posts the comparison
as a PR comment.
