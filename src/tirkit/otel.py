"""OpenTelemetry interop (SPEC FR-6): OTLP traces in, mapped onto Tirkit spans; Tirkit spans out.

Maps the OpenTelemetry GenAI semantic conventions (``gen_ai.*``, from the
open-telemetry/semantic-conventions-genai registry, checked 2026-11) and OpenInference attributes
(``openinference.span.kind``, ``llm.*``, ``input.value``...). Unmapped attributes are kept as Tirkit
attributes, so nothing is lost.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from tirkit.tracing import Span

_OPERATION_KIND = {
    "chat": "llm",
    "text_completion": "llm",
    "generate_content": "llm",
    "embeddings": "embedding",
    "execute_tool": "tool",
    "invoke_agent": "agent",
    "create_agent": "agent",
    "retrieval": "retrieval",
}
_OPENINFERENCE_KIND = {
    "LLM": "llm",
    "EMBEDDING": "embedding",
    "TOOL": "tool",
    "AGENT": "agent",
    "CHAIN": "chain",
    "RETRIEVER": "retrieval",
    "RERANKER": "rerank",
    "GUARDRAIL": "guardrail",
    "EVALUATOR": "evaluator",
}
_PARAMS = {
    "gen_ai.request.temperature": "temperature",
    "gen_ai.request.max_tokens": "max_tokens",
    "gen_ai.request.top_p": "top_p",
    "gen_ai.request.top_k": "top_k",
    "gen_ai.request.frequency_penalty": "frequency_penalty",
    "gen_ai.request.presence_penalty": "presence_penalty",
    "gen_ai.request.seed": "seed",
    "gen_ai.request.stop_sequences": "stop",
    "gen_ai.request.reasoning.level": "reasoning_level",
    "gen_ai.request.choice.count": "n",
}
_USAGE = {
    "gen_ai.usage.input_tokens": "input_tokens",
    "gen_ai.usage.output_tokens": "output_tokens",
    "gen_ai.usage.cache_read.input_tokens": "cached_input_tokens",
    "gen_ai.usage.cache_write.input_tokens": "cache_write_tokens",
    "gen_ai.usage.audio.input_tokens": "input_audio_tokens",
    "gen_ai.usage.audio.output_tokens": "output_audio_tokens",
    # Older conventions and OpenInference
    "gen_ai.usage.prompt_tokens": "input_tokens",
    "gen_ai.usage.completion_tokens": "output_tokens",
    "llm.token_count.prompt": "input_tokens",
    "llm.token_count.completion": "output_tokens",
    "llm.token_count.prompt_details.cache_read": "cached_input_tokens",
    "llm.token_count.prompt_details.audio": "input_audio_tokens",
}


def any_value(v: dict[str, Any]) -> Any:
    """Decode an OTLP AnyValue (JSON form: stringValue, intValue, kvlistValue...)."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [any_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {
            kv["key"]: any_value(kv.get("value", {})) for kv in v["kvlistValue"].get("values", [])
        }
    if "bytesValue" in v:
        return {"type": "bytes", "base64": v["bytesValue"]}
    return None


def _attrs(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {kv["key"]: any_value(kv.get("value", {})) for kv in items or []}


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _hex_id(value: str | None, from_proto: bool) -> str | None:
    if not value:
        return None
    if from_proto:  # protobuf JSON form base64-encodes bytes
        raw = base64.b64decode(value)
        return raw.hex() if raw.strip(b"\0") else None
    return value.lower() if value.strip("0") else None


def otlp_to_spans(payload: dict[str, Any], *, from_proto: bool = False) -> list[Span]:
    """Convert an OTLP ExportTraceServiceRequest (JSON shape) into Tirkit spans."""
    out: list[Span] = []
    for rs in payload.get("resourceSpans", []):
        resource = _attrs(rs.get("resource", {}).get("attributes"))
        for ss in rs.get("scopeSpans", []) or rs.get("instrumentationLibrarySpans", []):
            scope = (ss.get("scope") or ss.get("instrumentationLibrary") or {}).get("name")
            for sp in ss.get("spans", []):
                out.append(_span(sp, resource, scope, from_proto))
    return out


def _span(sp: dict[str, Any], resource: dict[str, Any], scope: str | None, proto: bool) -> Span:
    a = _attrs(sp.get("attributes"))
    trace_id = _hex_id(sp.get("traceId"), proto) or ""
    s = Span(
        name=sp.get("name") or "span",
        trace_id=trace_id,
        span_id=_hex_id(sp.get("spanId"), proto) or "",
        parent_id=_hex_id(sp.get("parentSpanId"), proto),
        start=int(sp.get("startTimeUnixNano", 0)) / 1e9,
        end=(int(sp["endTimeUnixNano"]) / 1e9) if sp.get("endTimeUnixNano") else None,
    )
    status = sp.get("status") or {}
    if status.get("code") in (2, "STATUS_CODE_ERROR"):
        s.record_error(status.get("message") or "error")

    op = a.pop("gen_ai.operation.name", None)
    oi_kind = a.pop("openinference.span.kind", None)
    s.kind = _OPERATION_KIND.get(str(op), "") or _OPENINFERENCE_KIND.get(str(oi_kind), "")
    if not s.kind:
        s.kind = "llm" if any(k.startswith("gen_ai.usage") for k in a) else "generic"

    s.model = (
        a.pop("gen_ai.response.model", None)
        or a.pop("gen_ai.request.model", None)
        or a.pop("llm.model_name", None)
    )
    a.pop("gen_ai.request.model", None)
    provider = a.pop("gen_ai.provider.name", None) or a.pop("gen_ai.system", None)
    if provider:
        s.attributes["provider"] = provider
    for key, unit in _USAGE.items():
        if key in a:
            value = a.pop(key)
            if isinstance(value, int | float) and unit not in s.usage:
                s.usage[unit] = float(value)
    for key, param in _PARAMS.items():
        if key in a:
            s.model_params[param] = a.pop(key)

    inp: dict[str, Any] = {}
    if "gen_ai.input.messages" in a:
        inp["messages"] = _maybe_json(a.pop("gen_ai.input.messages"))
    if "gen_ai.system_instructions" in a:
        inp["system"] = _maybe_json(a.pop("gen_ai.system_instructions"))
    if "gen_ai.tool.definitions" in a:
        inp["tools"] = _maybe_json(a.pop("gen_ai.tool.definitions"))
    if "gen_ai.tool.call.arguments" in a:
        inp["arguments"] = _maybe_json(a.pop("gen_ai.tool.call.arguments"))
    if "gen_ai.retrieval.query.text" in a:
        inp["query"] = a.pop("gen_ai.retrieval.query.text")
    if not inp and "input.value" in a:
        s.input = _maybe_json(a.pop("input.value"))
    elif inp:
        s.input = inp
    if "gen_ai.output.messages" in a:
        s.output = _maybe_json(a.pop("gen_ai.output.messages"))
    elif "gen_ai.tool.call.result" in a:
        s.output = _maybe_json(a.pop("gen_ai.tool.call.result"))
    elif "gen_ai.retrieval.documents" in a:
        s.output = _maybe_json(a.pop("gen_ai.retrieval.documents"))
    elif "output.value" in a:
        s.output = _maybe_json(a.pop("output.value"))
    a.pop("input.mime_type", None)
    a.pop("output.mime_type", None)

    s.session_id = a.pop("gen_ai.conversation.id", None) or a.pop("session.id", None) or None
    s.user_id = a.pop("user.id", None) or a.pop("enduser.id", None)
    if "gen_ai.response.time_to_first_chunk" in a:
        s.ttft_ms = float(a.pop("gen_ai.response.time_to_first_chunk")) * 1000
    if "gen_ai.response.finish_reasons" in a:
        s.attributes["finish_reason"] = a.pop("gen_ai.response.finish_reasons")
    if "gen_ai.response.id" in a:
        s.attributes["response_id"] = a.pop("gen_ai.response.id")
    s.release = resource.get("service.version")
    s.attributes.update(a)
    if resource.get("service.name"):
        s.attributes["service.name"] = resource["service.name"]
    if scope:
        s.attributes["otel.scope"] = scope
    return s


def decode_request(body: bytes, content_type: str) -> list[Span]:
    """Decode an OTLP/HTTP request body (protobuf or JSON)."""
    if "json" in content_type:
        return otlp_to_spans(json.loads(body or b"{}"), from_proto=False)
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    return otlp_to_spans(MessageToDict(req), from_proto=True)


# -- export -------------------------------------------------------------------------------------


def _kv(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "value": _to_any(value)}


def _to_any(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_to_any(v) for v in value]}}
    if isinstance(value, dict):
        return {"kvlistValue": {"values": [_kv(str(k), v) for k, v in value.items()]}}
    return {"stringValue": json.dumps(value, default=str)}


_KIND_TO_OPERATION = {v: k for k, v in _OPERATION_KIND.items() if k != "text_completion"}
_KIND_TO_OPERATION["llm"] = "chat"


def spans_to_otlp(spans: list[Span], service_name: str = "tirkit") -> dict[str, Any]:
    """Tirkit spans as an OTLP/JSON ExportTraceServiceRequest with GenAI attributes."""
    otlp_spans = []
    for s in spans:
        attrs: list[dict[str, Any]] = []
        if s.kind in _KIND_TO_OPERATION:
            attrs.append(_kv("gen_ai.operation.name", _KIND_TO_OPERATION[s.kind]))
        if s.model:
            attrs.append(_kv("gen_ai.request.model", s.model))
        rev_usage = {
            "input_tokens": "gen_ai.usage.input_tokens",
            "output_tokens": "gen_ai.usage.output_tokens",
            "cached_input_tokens": "gen_ai.usage.cache_read.input_tokens",
            "cache_write_tokens": "gen_ai.usage.cache_write.input_tokens",
            "input_audio_tokens": "gen_ai.usage.audio.input_tokens",
            "output_audio_tokens": "gen_ai.usage.audio.output_tokens",
        }
        for unit, value in s.usage.items():
            if unit in rev_usage:
                attrs.append(_kv(rev_usage[unit], int(value)))
        if s.session_id:
            attrs.append(_kv("gen_ai.conversation.id", s.session_id))
        if s.user_id:
            attrs.append(_kv("user.id", s.user_id))
        if s.input is not None:
            attrs.append(_kv("input.value", json.dumps(s.input, default=str)))
        if s.output is not None:
            attrs.append(_kv("output.value", json.dumps(s.output, default=str)))
        if s.cost_usd is not None:
            attrs.append(_kv("tirkit.cost_usd", s.cost_usd))
        span = {
            "traceId": s.trace_id.rjust(32, "0")[:32],
            "spanId": s.span_id.rjust(16, "0")[:16],
            "name": s.name,
            "kind": 3 if s.kind in {"llm", "embedding"} else 1,
            "startTimeUnixNano": str(int(s.start * 1e9)),
            "endTimeUnixNano": str(int((s.end or s.start) * 1e9)),
            "attributes": attrs,
            "status": {"code": 2, "message": s.error or ""} if s.status == "error" else {},
        }
        if s.parent_id:
            span["parentSpanId"] = s.parent_id.rjust(16, "0")[:16]
        otlp_spans.append(span)
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_kv("service.name", service_name)]},
                "scopeSpans": [{"scope": {"name": "tirkit"}, "spans": otlp_spans}],
            }
        ]
    }


class OTelSink:
    """Batch target that exports Tirkit spans to any OTLP/HTTP JSON endpoint
    (e.g. an OpenTelemetry Collector at ``http://localhost:4318/v1/traces``)."""

    def __init__(self, endpoint: str, headers: dict[str, str] | None = None) -> None:
        self.endpoint = endpoint
        self.headers = {"content-type": "application/json", **(headers or {})}

    def save_spans(self, spans: list[Span]) -> None:
        import httpx

        r = httpx.post(self.endpoint, json=spans_to_otlp(spans), headers=self.headers, timeout=30)
        r.raise_for_status()
