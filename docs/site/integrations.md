# Integrations

## OpenTelemetry (any language, any framework)

Tirkit accepts OTLP traces over HTTP (`POST /v1/traces`, protobuf or JSON) and, optionally, gRPC,
and maps the GenAI semantic conventions, OpenInference, OpenLLMetry and Vercel AI SDK attributes
onto spans, with cost, redaction, sessions and media handled like the SDK path. See
[Bring your own OpenTelemetry](opentelemetry.md) for setup, the Collector config and the mapping.

Tirkit can also *export* spans to any OTLP collector with `tirkit.otel.OTelSink(endpoint)`.

## Model providers

`tirkit.instrument()` patches every supported SDK that is installed; pass names to choose
(`tirkit.instrument("bedrock", "litellm")`).

| Name | Covers |
| --- | --- |
| `openai`, `anthropic`, `gemini` | See the [Python SDK](python-sdk.md) |
| `bedrock` | boto3 (and aioboto3) `converse`, `converse_stream`, `invoke_model`, `invoke_model_with_response_stream`; cache reads and writes; cost through cross-region profile ids (`us.amazon.nova-...`) |
| `mistral` | `chat.complete`, `chat.stream` (sync and async), `embeddings.create` |
| `cohere` | v2 client `chat`, `chat_stream`, `embed`, `rerank` (billed units; rerank counts search units) |
| `vertex` | The older `vertexai` SDK, `GenerativeModel.generate_content` (+ async, streaming). The newer `google-genai` SDK is covered by `gemini` |
| `litellm` | `litellm.completion`, `acompletion`, `embedding`, `aembedding`. One call is one span; spans for the provider SDK calls it makes are suppressed so cost is not counted twice. Call `tirkit.instrument()` before `from litellm import completion` |

## Agent and RAG frameworks

| Framework | How | Records |
| --- | --- | --- |
| OpenAI Agents SDK | `tirkit.instrument("openai_agents")` | Runs, agents, model calls (usage, cached and reasoning tokens), tools, MCP tools, handoffs, guardrails |
| LlamaIndex | `tirkit.instrument("llamaindex")` | Every instrumented call nested as LlamaIndex nests them; model and usage, retrieved nodes with scores, rerankers, embeddings, tool calls. Streamed LLM calls have no output or usage |
| DSPy | `tirkit.instrument("dspy")` | Modules, language-model calls, tools, `dspy.Evaluate` |
| CrewAI | `tirkit.instrument("crewai")` | Crew runs, tasks, agents, tools, model calls with usage. Tested against CrewAI 1.15 |
| Vercel AI SDK | Enable `experimental_telemetry` and send OTLP (see above) | `generateText`, `streamText`, `generateObject`, `embed`, tool calls, time to first chunk |
| LangChain, LangGraph | `TirkitCallbackHandler` (below) | Chains, models, tools, retrievers |

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
