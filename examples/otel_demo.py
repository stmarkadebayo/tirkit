"""Send GenAI spans to Tirkit with the standard OpenTelemetry SDK (no Tirkit code in the app).

    pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
    export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:8000/v1/traces
    export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Basic%20<base64 public:secret>"
    python examples/otel_demo.py

Any language's OpenTelemetry SDK works the same way; attributes follow the GenAI semantic
conventions, which Tirkit maps to model, tokens, cost, messages and sessions.
"""

from __future__ import annotations

import json
import time

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def main() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "otel-demo"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("otel-demo")
    for i in range(5):
        with tracer.start_as_current_span("invoke_agent support") as agent:
            agent.set_attribute("gen_ai.operation.name", "invoke_agent")
            agent.set_attribute("gen_ai.conversation.id", f"otel-conv-{i % 2}")
            with tracer.start_as_current_span("chat gpt-4o-mini") as span:
                span.set_attribute("gen_ai.operation.name", "chat")
                span.set_attribute("gen_ai.provider.name", "openai")
                span.set_attribute("gen_ai.request.model", "gpt-4o-mini")
                span.set_attribute("gen_ai.request.temperature", 0.2)
                span.set_attribute(
                    "gen_ai.input.messages",
                    json.dumps(
                        [
                            {
                                "role": "user",
                                "parts": [
                                    {"type": "text", "content": f"Question {i}: where is my order?"}
                                ],
                            }
                        ]
                    ),
                )
                time.sleep(0.05)
                span.set_attribute(
                    "gen_ai.output.messages",
                    json.dumps(
                        [
                            {
                                "role": "assistant",
                                "parts": [{"type": "text", "content": "It shipped yesterday."}],
                            }
                        ]
                    ),
                )
                span.set_attribute("gen_ai.usage.input_tokens", 42)
                span.set_attribute("gen_ai.usage.output_tokens", 7)
    provider.shutdown()
    print("sent 5 OpenTelemetry traces")


if __name__ == "__main__":
    main()
