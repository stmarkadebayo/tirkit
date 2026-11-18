"""OTLP attribute mapping for each convention (tirkit.otel.mapping), without a server.

The attribute names in these payloads are the ones the libraries emit; the parity tests in
test_otel_parity.py check them against the real instrumentation libraries where they are
installable in Python.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from tirkit.otel import otlp_to_spans, spans_to_otlp
from tirkit.tracing import Span

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 64).decode()


def _val(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, list):
        return {"arrayValue": {"values": [_val(x) for x in v]}}
    return {"stringValue": v}


def otlp(*spans: dict[str, Any], resource: dict[str, Any] | None = None) -> dict[str, Any]:
    out = []
    for i, sp in enumerate(spans):
        attrs = sp.get("attributes", {})
        item = {
            "traceId": sp.get("traceId", "ab" * 16),
            "spanId": sp.get("spanId", f"{i + 1:016x}"),
            "name": sp.get("name", "span"),
            "startTimeUnixNano": "1700000000000000000",
            "endTimeUnixNano": "1700000002000000000",
            "attributes": [{"key": k, "value": _val(v)} for k, v in attrs.items()],
            "events": [
                {
                    "name": e["name"],
                    "attributes": [
                        {"key": k, "value": _val(v)} for k, v in e.get("attributes", {}).items()
                    ],
                }
                for e in sp.get("events", [])
            ],
            "status": sp.get("status", {}),
        }
        if sp.get("parentSpanId"):
            item["parentSpanId"] = sp["parentSpanId"]
        out.append(item)
    res = [{"key": k, "value": _val(v)} for k, v in (resource or {}).items()]
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": res},
                "scopeSpans": [{"scope": {"name": "t"}, "spans": out}],
            }
        ]
    }


def one(**span: Any) -> Span:
    return otlp_to_spans(otlp(span))[0]


def test_vercel_ai_sdk_generate_text_and_tool_call() -> None:
    spans = otlp_to_spans(
        otlp(
            {
                "name": "ai.generateText",
                "spanId": "00000000000000a1",
                "attributes": {
                    "ai.operationId": "ai.generateText",
                    "operation.name": "ai.generateText weather",
                    "ai.model.id": "gpt-4o-mini",
                    "ai.model.provider": "openai.chat",
                    "ai.telemetry.functionId": "weather",
                    "ai.telemetry.metadata.sessionId": "sess-7",
                    "ai.telemetry.metadata.userId": "user-3",
                    "ai.telemetry.metadata.plan": "pro",
                    "ai.prompt": json.dumps({"prompt": "Weather in Paris?"}),
                    "ai.response.text": "Rainy.",
                    "ai.usage.promptTokens": 50,
                    "ai.usage.completionTokens": 4,
                    "ai.settings.maxRetries": 2,
                },
            },
            {
                "name": "ai.generateText.doGenerate",
                "parentSpanId": "00000000000000a1",
                "attributes": {
                    "ai.operationId": "ai.generateText.doGenerate",
                    "ai.model.id": "gpt-4o-mini",
                    "ai.model.provider": "openai.chat",
                    "ai.prompt.messages": json.dumps(
                        [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "Weather in Paris?"},
                                    {"type": "image", "image": PNG, "mimeType": "image/png"},
                                ],
                            }
                        ]
                    ),
                    "ai.response.text": "Rainy.",
                    "ai.response.finishReason": "stop",
                    "ai.response.msToFirstChunk": 123.0,
                    "ai.usage.inputTokens": 50,
                    "ai.usage.outputTokens": 4,
                    "ai.usage.reasoningTokens": 2,
                    "ai.settings.temperature": 0.3,
                },
            },
            {
                "name": "ai.toolCall",
                "parentSpanId": "00000000000000a1",
                "attributes": {
                    "ai.operationId": "ai.toolCall",
                    "ai.toolCall.name": "getWeather",
                    "ai.toolCall.id": "call_1",
                    "ai.toolCall.args": '{"city": "Paris"}',
                    "ai.toolCall.result": '{"temp": 12}',
                },
            },
        )
    )
    outer, call, tool = spans
    assert outer.kind == "chain" and outer.input == {"prompt": "Weather in Paris?"}
    assert outer.session_id == "sess-7" and outer.user_id == "user-3"
    assert outer.attributes["metadata"] == {"plan": "pro"}
    assert outer.attributes["provider"] == "openai" and outer.model == "gpt-4o-mini"
    assert call.kind == "llm" and call.output == "Rainy." and call.ttft_ms == 123.0
    assert call.usage == {"input_tokens": 50, "output_tokens": 4, "reasoning_tokens": 2}
    assert call.model_params == {"temperature": 0.3}
    image = call.input["messages"][0]["content"][1]["image"]
    assert image == f"data:image/png;base64,{PNG}"  # media extraction picks this up
    assert call.attributes["finish_reason"] == "stop"
    assert tool.kind == "tool" and tool.attributes["tool_name"] == "getWeather"
    assert tool.input == {"arguments": {"city": "Paris"}} and tool.output == {"temp": 12}
    assert "ai.operationId" not in call.attributes


def test_openinference_flattened_messages_tools_and_documents() -> None:
    s = one(
        name="ChatCompletion",
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4o-mini-2024-07-18",
            "llm.provider": "openai",
            "llm.invocation_parameters": '{"model": "gpt-4o-mini", "temperature": 0.2}',
            "llm.input_messages.0.message.role": "system",
            "llm.input_messages.0.message.content": "Be brief",
            "llm.input_messages.1.message.role": "user",
            "llm.input_messages.1.message.contents.0.message_content.type": "text",
            "llm.input_messages.1.message.contents.0.message_content.text": "Weather?",
            "llm.input_messages.1.message.contents.1.message_content.type": "image",
            "llm.input_messages.1.message.contents.1.message_content.image.image.url": "data:image/png;base64,"
            + PNG,
            "llm.output_messages.0.message.role": "assistant",
            "llm.output_messages.0.message.content": "Rainy",
            "llm.token_count.prompt": 120,
            "llm.token_count.completion": 30,
            "llm.token_count.total": 150,
            "llm.token_count.prompt_details.cache_read": 100,
            "llm.token_count.completion_details.reasoning": 7,
            "session.id": "s-1",
            "user.id": "u-1",
            "tag.tags": ["prod", "eu"],
            "metadata": '{"tenant": "acme"}',
            "input.value": "{...}",
            "input.mime_type": "application/json",
        },
    )
    assert s.kind == "llm" and s.model == "gpt-4o-mini-2024-07-18"
    assert s.input["messages"][0] == {"role": "system", "content": "Be brief"}
    user = s.input["messages"][1]
    assert user["content"][0] == {"type": "text", "text": "Weather?"}
    assert user["content"][1]["image"]["image"]["url"].startswith("data:image/png;base64,")
    assert s.output == [{"role": "assistant", "content": "Rainy"}]
    assert s.usage == {
        "input_tokens": 120,
        "output_tokens": 30,
        "cached_input_tokens": 100,
        "reasoning_tokens": 7,
    }
    assert s.model_params == {"temperature": 0.2}
    assert (s.session_id, s.user_id, s.tags) == ("s-1", "u-1", ["prod", "eu"])
    assert s.attributes["metadata"] == {"tenant": "acme"}
    assert not any(k.startswith("llm.") for k in s.attributes)


def test_openinference_retriever_and_reranker() -> None:
    r = one(
        attributes={
            "openinference.span.kind": "RETRIEVER",
            "input.value": "returns policy",
            "retrieval.documents.0.document.content": "30 days",
            "retrieval.documents.0.document.score": 0.9,
            "retrieval.documents.1.document.content": "receipt needed",
        }
    )
    assert r.kind == "retrieval" and r.input == "returns policy"
    assert r.output == [{"content": "30 days", "score": 0.9}, {"content": "receipt needed"}]
    rr = one(
        attributes={
            "openinference.span.kind": "RERANKER",
            "reranker.query": "q",
            "reranker.model_name": "rerank-v3",
            "reranker.input_documents.0.document.content": "a",
            "reranker.output_documents.0.document.content": "a",
            "reranker.output_documents.0.document.score": 0.8,
        }
    )
    assert rr.kind == "rerank" and rr.input == {"query": "q", "documents": [{"content": "a"}]}
    assert rr.output == [{"content": "a", "score": 0.8}]


def test_openllmetry_legacy_indexed_prompts_and_workflow_kinds() -> None:
    s = one(
        name="openai.chat",
        attributes={
            "llm.request.type": "chat",
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o-mini",
            "gen_ai.prompt.0.role": "user",
            "gen_ai.prompt.0.content": "Hi",
            "gen_ai.completion.0.role": "assistant",
            "gen_ai.completion.0.content": "Hello",
            "gen_ai.completion.0.finish_reason": "stop",
            "gen_ai.usage.prompt_tokens": 9,
            "gen_ai.usage.completion_tokens": 2,
            "gen_ai.usage.cache_read_input_tokens": 4,
            "llm.usage.total_tokens": 11,
            "traceloop.association.properties.session_id": "conv-9",
            "traceloop.association.properties.customer": "acme",
        },
    )
    assert s.kind == "llm" and s.attributes["provider"] == "openai"
    assert s.input == {"messages": [{"role": "user", "content": "Hi"}]}
    assert s.output == [{"role": "assistant", "content": "Hello", "finish_reason": "stop"}]
    assert s.usage == {"input_tokens": 9, "output_tokens": 2, "cached_input_tokens": 4}
    assert s.session_id == "conv-9" and s.attributes["metadata"] == {"customer": "acme"}
    wf = one(
        name="plan.workflow",
        attributes={
            "traceloop.span.kind": "workflow",
            "traceloop.entity.name": "plan",
            "traceloop.entity.input": '{"inputs": {"q": "x"}}',
            "traceloop.entity.output": '"done"',
        },
    )
    assert wf.kind == "chain" and wf.input == {"inputs": {"q": "x"}} and wf.output == "done"


def test_genai_span_events_and_exception() -> None:
    s = one(
        name="chat gpt-4o",
        status={"code": 2},
        attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o"},
        events=[
            {"name": "gen_ai.system.message", "attributes": {"content": "Be brief"}},
            {"name": "gen_ai.user.message", "attributes": {"content": "Hi"}},
            {"name": "gen_ai.choice", "attributes": {"index": 0, "finish_reason": "stop"}},
            {
                "name": "exception",
                "attributes": {"exception.type": "RateLimitError", "exception.message": "429"},
            },
        ],
    )
    assert s.input == {
        "messages": [{"role": "system", "content": "Be brief"}, {"role": "user", "content": "Hi"}]
    }
    assert s.output == [{"index": 0, "finish_reason": "stop"}]
    assert s.status == "error" and s.error == "RateLimitError: 429"


def test_genai_blob_parts_become_data_uris() -> None:
    msgs = [
        {
            "role": "user",
            "parts": [
                {"type": "text", "content": "What is this?"},
                {"type": "blob", "modality": "image", "mime_type": "image/png", "content": PNG},
            ],
        }
    ]
    s = one(attributes={"gen_ai.operation.name": "chat", "gen_ai.input.messages": json.dumps(msgs)})
    assert s.input["messages"][0]["parts"][1]["content"] == f"data:image/png;base64,{PNG}"
    assert s.input["messages"][0]["parts"][0]["content"] == "What is this?"


def test_new_kinds_mcp_vector_db_and_time_to_first_chunk() -> None:
    mcp = one(
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "mcp.method.name": "tools/call",
            "gen_ai.tool.name": "search_docs",
        }
    )
    assert mcp.kind == "mcp_tool" and mcp.attributes["tool_name"] == "search_docs"
    vdb = one(
        attributes={"db.system": "chroma", "db.operation": "query", "db.vector.query.top_k": 4}
    )
    assert vdb.kind == "vector_db" and vdb.attributes["db.vector.query.top_k"] == 4
    llm = one(
        attributes={"gen_ai.operation.name": "chat", "gen_ai.response.time_to_first_chunk": 0.25}
    )
    assert llm.ttft_ms == 250.0


def test_round_trip_keeps_new_kinds_reasoning_and_ttft() -> None:
    s = Span(name="r", kind="vector_db", usage={"input_tokens": 3, "reasoning_tokens": 1.0})
    s.ttft_ms = 40.0
    s.end = s.start + 1
    back = otlp_to_spans(spans_to_otlp([s]))[0]
    assert back.kind == "vector_db" and back.ttft_ms == 40.0
    assert back.usage == {"input_tokens": 3, "reasoning_tokens": 1}


def test_session_spreads_to_spans_of_the_same_trace_in_a_batch() -> None:
    spans = otlp_to_spans(
        otlp(
            {"name": "root", "spanId": "00000000000000a1", "attributes": {}},
            {
                "name": "child",
                "parentSpanId": "00000000000000a1",
                "attributes": {"session.id": "s-9", "user.id": "u-9"},
            },
            {"name": "other", "traceId": "cd" * 16, "attributes": {}},
        )
    )
    root, child, other = spans
    assert (root.session_id, root.user_id) == ("s-9", "u-9")
    assert other.session_id is None
