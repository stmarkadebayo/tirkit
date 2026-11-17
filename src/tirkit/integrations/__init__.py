"""Framework integrations (LangChain, Hugging Face transformers). Each imports its framework lazily.

Frameworks with OpenTelemetry instrumentation (LlamaIndex, CrewAI, Haystack, DSPy, Vercel AI SDK,
OpenLLMetry's 20+ provider instrumentations) can send to Tirkit directly over OTLP:
``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=<tirkit>/v1/traces`` with a Basic auth header.
"""
