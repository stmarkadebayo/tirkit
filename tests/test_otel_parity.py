"""OTLP parity: spans sent by real OpenTelemetry exporters get what the SDK path gets
(attribute mapping, cost, sessions, media from base64 attributes, redaction).

Exporters: Python OTLP/HTTP (protobuf) and OTLP/gRPC, and Node OTLP/HTTP (needs ``npm ci`` in
tests/otel_node). Instrumentations: OpenInference and OpenLLMetry for the OpenAI SDK, and the
official OpenTelemetry one when it imports. The OpenAI client talks to a mocked transport, so
nothing leaves the machine.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.server

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("fastapi")

import httpx  # noqa: E402

from tests.test_server import PASSWORD, make_settings  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 2048
PNG_B64 = base64.b64encode(PNG).decode()
NODE_DIR = Path(__file__).parent / "otel_node"


def _basic(pk: str, sk: str) -> str:
    return "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()


def _project(c: Any, redact: bool = True) -> tuple[str, str]:
    """Admin, project, API key and redaction on. Returns (project_id, Authorization header)."""
    r = c.post("/api/setup", json={"email": "admin@example.com", "password": PASSWORD})
    assert r.status_code == 200, r.text
    pid = r.json()["project_id"]
    c.headers["X-CSRF-Token"] = c.get("/api/auth/me").json()["csrf"]
    if redact:
        assert c.put(f"/api/projects/{pid}/settings", json={"redact": True}).status_code == 200
    key = c.post(f"/api/projects/{pid}/keys", json={"name": "otel"}).json()
    return pid, _basic(key["public_key"], key["secret_key"])


@pytest.fixture
def live(live_server) -> Iterator[tuple[str, httpx.Client, str, str]]:
    url, _ = live_server
    with httpx.Client(base_url=url) as c:
        pid, auth = _project(c)
        yield url, c, pid, auth


def _tracer_provider(endpoint: str, auth: str) -> Any:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    tp = TracerProvider(resource=Resource.create({"service.name": "shop", "service.version": "v2"}))
    exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers={"authorization": auth})
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp


CHAT = {
    "id": "c1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-4o-mini-2024-07-18",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Rainy, write to help@example.com"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 100},
        "completion_tokens_details": {"reasoning_tokens": 7},
    },
}


def _openai_client() -> Any:
    openai = pytest.importorskip("openai")
    httpx2 = pytest.importorskip("httpx2")
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json=CHAT))
    return openai.OpenAI(api_key="k", http_client=httpx2.Client(transport=transport))


def _messages(with_image: bool) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": "Weather? I am x.y@example.com"}]
    if with_image:
        url = f"data:image/png;base64,{PNG_B64}"
        content.append({"type": "image_url", "image_url": {"url": url}})
    return [{"role": "system", "content": "Be brief"}, {"role": "user", "content": content}]


def _spans(c: httpx.Client, pid: str) -> list[dict[str, Any]]:
    return list(c.get(f"/api/projects/{pid}/spans").json()["items"])


def _media_refs(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        if node.get("type") == "media" and "sha256" in node:
            return [node]
        return [r for v in node.values() for r in _media_refs(v)]
    if isinstance(node, list):
        return [r for v in node for r in _media_refs(v)]
    return []


def _check_llm_span(s: dict[str, Any]) -> None:
    assert s["kind"] == "llm" and s["model"] == "gpt-4o-mini-2024-07-18"
    assert s["usage"]["input_tokens"] == 120 and s["usage"]["output_tokens"] == 30
    assert s["usage"]["cached_input_tokens"] == 100
    assert s["usage"]["reasoning_tokens"] == 7
    assert s["cost_usd"] and s["cost_usd"] > 0  # priced from the model, as on the SDK path
    assert s["attributes"]["service.name"] == "shop" and s["release"] == "v2"
    text = json.dumps([s["input"], s["output"]])
    assert "example.com" not in text and "[EMAIL]" in text  # project redaction applied


def test_openinference_over_otlp_http(live) -> None:
    oi = pytest.importorskip("openinference.instrumentation.openai")
    from openinference.instrumentation import using_session, using_user

    url, c, pid, auth = live
    tp = _tracer_provider(url, auth)
    instrumentor = oi.OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=tp)
    try:
        with using_session("sess-oi"), using_user("user-oi"):
            _openai_client().chat.completions.create(
                model="gpt-4o-mini", messages=_messages(with_image=True), temperature=0.2
            )
    finally:
        instrumentor.uninstrument()
        tp.shutdown()
    (s,) = _spans(c, pid)
    _check_llm_span(s)
    assert s["session_id"] == "sess-oi" and s["user_id"] == "user-oi"
    assert s["model_params"] == {"temperature": 0.2}
    assert s["input"]["messages"][0] == {"role": "system", "content": "Be brief"}
    # The base64 image was moved to the media store, like the SDK path does.
    (ref,) = _media_refs(s["input"])
    assert ref["mime"] == "image/png" and PNG_B64 not in json.dumps(s)
    got = c.get(f"/api/projects/{pid}/media/{ref['sha256']}")
    assert got.status_code == 200 and got.content == PNG
    sessions = c.get(f"/api/projects/{pid}/sessions").json()
    assert "sess-oi" in json.dumps(sessions)


def test_openllmetry_over_otlp_http(live) -> None:
    tl = pytest.importorskip("opentelemetry.instrumentation.openai")
    from opentelemetry import trace as otel_trace

    url, c, pid, auth = live
    tp = _tracer_provider(url, auth)
    instrumentor = tl.OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=tp)
    try:
        tracer = tp.get_tracer("app")
        with tracer.start_as_current_span("handle", kind=otel_trace.SpanKind.SERVER) as root:
            root.set_attribute("gen_ai.conversation.id", "conv-tl")
            _openai_client().chat.completions.create(
                model="gpt-4o-mini", messages=_messages(with_image=False)
            )
    finally:
        instrumentor.uninstrument()
        tp.shutdown()
    spans = {s["name"]: s for s in _spans(c, pid)}
    llm = spans["openai.chat"]
    _check_llm_span(llm)
    assert llm["input"]["messages"][1]["parts"][0]["type"] == "text"
    assert llm["output"][0]["parts"][0]["content"].startswith("Rainy")
    trace = c.get(f"/api/projects/{pid}/traces/{llm['trace_id']}").json()
    assert trace["trace"]["session_id"] == "conv-tl"


def test_official_openai_v2_over_otlp_http(live) -> None:
    try:
        from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
    except Exception as exc:  # noqa: BLE001 - the beta package breaks on some util-genai versions
        pytest.skip(f"opentelemetry-instrumentation-openai-v2 does not import here: {exc}")
    url, c, pid, auth = live
    tp = _tracer_provider(url, auth)
    instrumentor = OpenAIInstrumentor()
    try:
        instrumentor.instrument(tracer_provider=tp)
    except Exception as exc:  # noqa: BLE001
        tp.shutdown()
        pytest.skip(f"opentelemetry-instrumentation-openai-v2 cannot instrument here: {exc}")
    try:
        _openai_client().chat.completions.create(
            model="gpt-4o-mini", messages=_messages(with_image=False), temperature=0.2
        )
    finally:
        instrumentor.uninstrument()
        tp.shutdown()
    (s,) = _spans(c, pid)
    assert s["kind"] == "llm" and s["model"] == "gpt-4o-mini-2024-07-18"
    assert s["usage"]["input_tokens"] == 120 and s["cost_usd"] > 0
    assert s["model_params"]["temperature"] == 0.2


# -- gRPC -----------------------------------------------------------------------------------


@pytest.fixture
def grpc_app(tmp_path) -> Iterator[tuple[Any, int, str, str]]:
    pytest.importorskip("grpc")
    from fastapi.testclient import TestClient

    from tirkit.server.app import create_app

    settings = make_settings(tmp_path)
    settings.otlp_grpc_port = 0
    with TestClient(create_app(settings)) as c:
        pid, auth = _project(c, redact=False)
        yield c, c.app.state.otlp_grpc_port, pid, auth


def _grpc_exporter(port: int, auth: str) -> Any:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(
        endpoint=f"127.0.0.1:{port}", insecure=True, headers=(("authorization", auth),)
    )


def test_otlp_grpc_receiver_with_real_exporter(grpc_app) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    c, port, pid, auth = grpc_app
    memory = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(memory))
    parts = [
        {"type": "text", "content": "What is this?"},
        {"type": "blob", "modality": "image", "mime_type": "image/png", "content": PNG_B64},
    ]
    with tp.get_tracer("app").start_as_current_span("chat gpt-4o") as sp:
        sp.set_attribute("gen_ai.operation.name", "chat")
        sp.set_attribute("gen_ai.request.model", "gpt-4o")
        sp.set_attribute("gen_ai.usage.input_tokens", 10)
        sp.set_attribute("gen_ai.usage.output_tokens", 5)
        sp.set_attribute("gen_ai.conversation.id", "conv-grpc")
        sp.set_attribute("gen_ai.input.messages", json.dumps([{"role": "user", "parts": parts}]))
    tp.shutdown()
    finished = memory.get_finished_spans()

    exporter = _grpc_exporter(port, auth)
    assert exporter.export(finished) == SpanExportResult.SUCCESS
    exporter.shutdown()
    (s,) = c.get(f"/api/projects/{pid}/spans").json()["items"]
    assert s["kind"] == "llm" and s["session_id"] == "conv-grpc" and s["cost_usd"] > 0
    (ref,) = _media_refs(s["input"])
    assert c.get(f"/api/projects/{pid}/media/{ref['sha256']}").content == PNG

    bad = _grpc_exporter(port, _basic("pk-tirkit-nope", "sk-tirkit-nope"))
    assert bad.export(finished) == SpanExportResult.FAILURE
    bad.shutdown()


# -- Node -----------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_otlp_http_exporter_and_vercel_ai_sdk_attributes(live) -> None:
    if not (NODE_DIR / "node_modules").exists():
        pytest.skip("run `npm ci` in tests/otel_node to enable the Node exporter test")
    url, c, pid, auth = live
    subprocess.run(
        ["node", str(NODE_DIR / "export.mjs"), f"{url}/v1/traces", auth, PNG_B64],
        check=True,
        timeout=60,
        capture_output=True,
    )
    spans = {s["name"]: s for s in _spans(c, pid)}
    outer = spans["ai.generateText"]
    call = spans["ai.generateText.doGenerate"]
    assert outer["kind"] == "chain" and outer["session_id"] == "sess-node"
    assert call["kind"] == "llm" and call["model"] == "gpt-4o-mini"
    assert call["usage"] == {"input_tokens": 50, "output_tokens": 4} and call["cost_usd"] > 0
    assert call["ttft_ms"] == 120 and call["attributes"]["service.name"] == "node-app"
    (ref,) = _media_refs(call["input"])
    assert c.get(f"/api/projects/{pid}/media/{ref['sha256']}").content == PNG
    assert "[EMAIL]" in json.dumps(call["input"])
