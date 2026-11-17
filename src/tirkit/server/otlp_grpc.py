"""Optional OTLP/gRPC trace receiver on its own port (default off).

Turn it on with ``TIRKIT_OTLP_GRPC_PORT=4317`` (or ``tirkit serve --otlp-grpc-port 4317``). It
needs ``pip install "tirkit[otlp-grpc]"``. Exporters authenticate with the same API keys as
OTLP/HTTP, sent as gRPC metadata: ``authorization: Basic base64(pk:sk)``. Spans go through the
same path as ``POST /v1/traces``: attribute mapping, media extraction, cost and redaction.

The port speaks plain gRPC (no TLS); put it behind a TLS proxy or an OpenTelemetry Collector
when it leaves the machine.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException

from tirkit.server.deps import key_project
from tirkit.server.routers.ingest import save_otlp_spans


@dataclass
class _GrpcCall:
    """What the shared ingestion helpers read from a request: the app and the headers."""

    app: Any
    headers: dict[str, str] = field(default_factory=dict)


def make_servicer(app: FastAPI) -> Any:
    import grpc
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

    from tirkit.otel import otlp_to_spans

    class TraceService(trace_service_pb2_grpc.TraceServiceServicer):  # type: ignore[misc]
        def Export(self, request: Any, context: Any) -> Any:  # noqa: N802 - gRPC method name
            headers = {k.lower(): v for k, v in context.invocation_metadata() or ()}
            call = _GrpcCall(app, headers)
            try:
                pid = key_project(call)  # type: ignore[arg-type]
            except HTTPException:
                context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid API key")
            try:
                spans = otlp_to_spans(MessageToDict(request), from_proto=True)
                save_otlp_spans(call, pid, spans)
            except HTTPException as exc:
                code = (
                    grpc.StatusCode.RESOURCE_EXHAUSTED
                    if exc.status_code == 429
                    else grpc.StatusCode.INVALID_ARGUMENT
                )
                context.abort(code, str(exc.detail))
            return trace_service_pb2.ExportTraceServiceResponse()

    return TraceService()


def start_grpc_receiver(app: FastAPI, port: int, host: str = "127.0.0.1") -> tuple[Any, int]:
    """Start the receiver in background threads. Returns ``(server, bound_port)``; pass port 0
    to pick a free port. Stop it with ``server.stop(grace)``."""
    try:
        import grpc
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError('OTLP/gRPC needs: pip install "tirkit[otlp-grpc]"') from exc

    limit = app.state.settings.max_ingest_bytes
    server = grpc.server(
        ThreadPoolExecutor(max_workers=8, thread_name_prefix="tirkit-otlp-grpc"),
        options=[("grpc.max_receive_message_length", limit)],
    )
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(make_servicer(app), server)
    bound = server.add_insecure_port(f"{host}:{port}")
    if not bound:
        raise RuntimeError(f"could not bind OTLP/gRPC receiver to {host}:{port}")
    server.start()
    return server, bound
