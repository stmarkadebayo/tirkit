# Bring your own OpenTelemetry

You do not need the Tirkit SDK. Any OpenTelemetry exporter, in any language, can send traces to
Tirkit, and the spans get the same treatment as spans from the SDK: model, token usage and cost,
sessions and users, prompt-cache and reasoning tokens, time to first token, images and audio moved
to the media store, and the project's redaction setting.

## 1. Create an API key

In the UI, **Settings → API keys**, or `POST /api/projects/{id}/keys`. Build the Basic auth value
once:

```bash
export TIRKIT_BASIC_AUTH=$(printf '%s:%s' "$TIRKIT_PUBLIC_KEY" "$TIRKIT_SECRET_KEY" | base64)
```

## 2. Point your exporter at Tirkit

=== "Environment variables (any language)"

    ```bash
    export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:8000/v1/traces
    export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
    export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Basic%20$TIRKIT_BASIC_AUTH"
    ```

=== "Python"

    ```python
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "shop", "service.version": "v2"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint="http://localhost:8000/v1/traces",
        headers={"authorization": f"Basic {TIRKIT_BASIC_AUTH}"},
    )))
    trace.set_tracer_provider(provider)
    ```

=== "Node.js"

    ```js
    import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";

    const exporter = new OTLPTraceExporter({
      url: "http://localhost:8000/v1/traces",
      headers: { authorization: `Basic ${process.env.TIRKIT_BASIC_AUTH}` },
    });
    ```

`service.name` is kept as an attribute and `service.version` becomes the span's **release**.

## 3. Add a GenAI instrumentation library

Pick the one for your framework; Tirkit reads all of these:

| Library | Convention it emits | Notes |
| --- | --- | --- |
| [OpenInference](https://github.com/Arize-ai/openinference) (`openinference-instrumentation-*`) | `openinference.span.kind`, `llm.input_messages.N.*`, `llm.token_count.*` | Sessions and users through `using_session()` and `using_user()` |
| [OpenLLMetry / Traceloop](https://github.com/traceloop/openllmetry) (`opentelemetry-instrumentation-openai` and 30 more) | `gen_ai.*`, older `gen_ai.prompt.N.*`, `traceloop.*` | Records messages unless `TRACELOOP_TRACE_CONTENT=false` |
| OpenTelemetry's own `opentelemetry-instrumentation-openai-v2` and the `gen_ai.*` semantic conventions | `gen_ai.*`, `gen_ai.input.messages`, span events | Content capture is opt-in (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`) |
| Vercel AI SDK (`experimental_telemetry: { isEnabled: true }`) | `ai.*` | `metadata: { sessionId, userId }` sets the session and user; inputs and outputs are recorded unless you set `recordInputs` or `recordOutputs` to false |
| LlamaIndex, CrewAI, DSPy, Haystack, Spring AI | one of the above (via OpenInference or OpenLLMetry) | Python users can also use the direct integrations in [Integrations](integrations.md) |

The attribute names each library uses are listed in one file, `src/tirkit/otel/mapping.py`. A
library that is missing from it still works: unmapped attributes are kept on the span as
attributes, so nothing is lost. To add one, add its names to the tables in that file and a case to
`tests/test_otel_mapping.py`.

### What is mapped

| Tirkit field | OpenTelemetry sources |
| --- | --- |
| kind | `gen_ai.operation.name` (chat, embeddings, execute_tool, invoke_agent, retrieval, rerank), `openinference.span.kind`, `traceloop.span.kind`, `llm.request.type`, `ai.operationId`; `mcp.method.name` gives `mcp_tool`, a vector database in `db.system` gives `vector_db` |
| model, provider | `gen_ai.response.model` then `gen_ai.request.model`, `llm.model_name`, `ai.model.id`; `gen_ai.provider.name`, `gen_ai.system`, `llm.provider` |
| usage | input, output, cached (`cache_read`), cache write, audio, and reasoning tokens, under every name above |
| input, output | messages, system instructions, tool definitions, tool arguments and results, retrieval query and documents, reranker documents; from attributes, indexed attributes (`gen_ai.prompt.0.content`) or span events |
| session, user, tags, metadata | `gen_ai.conversation.id`, `session.id`, `user.id`, `langfuse.*`, `traceloop.association.properties.*`, `ai.telemetry.metadata.*` |
| time to first token | `gen_ai.response.time_to_first_chunk` (seconds), `ai.response.msToFirstChunk` (ms) |
| error | span status, and the `exception` event |

The session and user are often set on only one span; Tirkit copies them to the other spans of the
same trace in a request.

### Cost, redaction and media

- **Cost** is computed at ingest from the model and usage, with your project's price overrides.
- **Redaction** follows the project setting, the same as for the SDK.
- **Images and audio** in base64 (`data:` URIs, GenAI `blob` parts, Vercel AI SDK `image` parts and
  OpenInference image URLs) are moved to the media store and shown in the trace view. Media that
  your instrumentation does not put in the span (most libraries drop it unless asked) cannot be
  recovered: turn on content or image capture in the library if you want to see it.

## Collector

Run a Collector next to your apps when you want batching, retries or several backends. A complete
config is in `examples/otel-collector.yaml`:

```yaml
exporters:
  otlphttp/tirkit:
    endpoint: http://tirkit:8000
    headers:
      Authorization: "Basic ${env:TIRKIT_BASIC_AUTH}"
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp/tirkit]
```

The `otlphttp` exporter appends `/v1/traces`. Spans larger than 20 MB per request are refused, so
keep the `batch` processor's `send_batch_max_size` modest if you capture large media.

## OTLP over gRPC (optional)

The server also speaks OTLP/gRPC on its own port, off by default:

```bash
pip install "tirkit[otlp-grpc]"
tirkit serve --otlp-grpc-port 4317          # or TIRKIT_OTLP_GRPC_PORT=4317
```

Authenticate with the same keys, as gRPC metadata `authorization: Basic <base64>`. The port is
plain gRPC without TLS and binds to the server's host (`TIRKIT_OTLP_GRPC_HOST` to change it): put
it behind a TLS proxy or a Collector when it leaves the machine. It needs a single server
process, so it is not available with `--workers` greater than 1.

```python
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
OTLPSpanExporter(endpoint="tirkit:4317", insecure=True, headers=(("authorization", f"Basic {TIRKIT_BASIC_AUTH}"),))
```

## What is not covered

- Metrics and logs: only traces are accepted.
- OTLP over gRPC is a single-process feature of `tirkit serve`; the Docker image exposes HTTP only
  until you add the port yourself.
