"""The one place that maps OpenTelemetry attribute names onto Tirkit span fields.

GenAI instrumentation libraries disagree on names. Each table below lists every spelling we
know for one Tirkit field; :func:`apply` pops the ones it understands and leaves the rest as
attributes, so nothing is lost. Conventions covered (attribute names checked against the
libraries' output in ``tests/test_otel_parity.py``):

- OpenTelemetry GenAI semantic conventions, current (``gen_ai.input.messages``...) and older
  (``gen_ai.prompt.0.content``, ``gen_ai.usage.prompt_tokens``, span events).
- OpenInference (Arize Phoenix): ``openinference.span.kind``, ``llm.input_messages.0.message.*``.
- OpenLLMetry (Traceloop): ``traceloop.span.kind``, ``traceloop.entity.*``, ``llm.request.type``.
- Vercel AI SDK (``experimental_telemetry``): ``ai.operationId``, ``ai.prompt.messages``...
- Session and user ids from Langfuse-style and Traceloop association attributes.

To support another library, add its names to these tables and a case to the parity tests.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from tirkit.types import Span

# -- span kind --------------------------------------------------------------------------------

GENAI_OPERATION_KIND = {
    "chat": "llm",
    "text_completion": "llm",
    "generate_content": "llm",
    "embeddings": "embedding",
    "execute_tool": "tool",
    "invoke_agent": "agent",
    "create_agent": "agent",
    "invoke_workflow": "chain",
    "retrieval": "retrieval",
    "rerank": "rerank",
}
OPENINFERENCE_KIND = {
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
TRACELOOP_KIND = {"workflow": "chain", "task": "chain", "agent": "agent", "tool": "tool"}
TRACELOOP_REQUEST_TYPE = {
    "chat": "llm",
    "completion": "llm",
    "embedding": "embedding",
    "rerank": "rerank",
}
# Vercel AI SDK operation ids (the span name is the same). Outer calls are chains; the
# ``do*`` spans are the actual provider calls.
VERCEL_KIND = {
    "ai.generateText": "chain",
    "ai.streamText": "chain",
    "ai.generateObject": "chain",
    "ai.streamObject": "chain",
    "ai.embed": "chain",
    "ai.embedMany": "chain",
    "ai.generateText.doGenerate": "llm",
    "ai.streamText.doStream": "llm",
    "ai.generateObject.doGenerate": "llm",
    "ai.streamObject.doStream": "llm",
    "ai.embed.doEmbed": "embedding",
    "ai.embedMany.doEmbed": "embedding",
    "ai.toolCall": "tool",
}
VECTOR_DBS = {
    "chroma",
    "chromadb",
    "pinecone",
    "qdrant",
    "weaviate",
    "milvus",
    "pgvector",
    "lancedb",
    "marqo",
    "elasticsearch",
    "opensearch",
    "redis",
    "mongodb",
}

# -- fields with several spellings (first present wins) ----------------------------------------

MODEL = (
    "gen_ai.response.model",
    "gen_ai.request.model",
    "llm.model_name",
    "llm.response.model",
    "llm.request.model",
    "ai.response.model",
    "ai.model.id",
)
PROVIDER = (
    "gen_ai.provider.name",
    "gen_ai.system",
    "llm.provider",
    "llm.system",
    "ai.model.provider",
)
SESSION = (
    "gen_ai.conversation.id",
    "session.id",
    "langfuse.session.id",
    "traceloop.association.properties.session_id",
    "ai.telemetry.metadata.sessionId",
    "ai.telemetry.metadata.session_id",
)
USER = (
    "user.id",
    "enduser.id",
    "langfuse.user.id",
    "traceloop.association.properties.user_id",
    "ai.telemetry.metadata.userId",
    "ai.telemetry.metadata.user_id",
    "gen_ai.user",
)
TOOL_NAME = ("gen_ai.tool.name", "tool.name", "ai.toolCall.name")
TAGS = ("tag.tags", "langfuse.trace.tags", "ai.telemetry.metadata.tags")
# Time to first token: (attribute, multiplier to milliseconds)
TTFT = (
    ("gen_ai.response.time_to_first_chunk", 1000.0),
    ("gen_ai.server.time_to_first_token", 1000.0),
    ("ai.response.msToFirstChunk", 1.0),
    ("ai.stream.msToFirstChunk", 1.0),
)
FINISH_REASON = ("gen_ai.response.finish_reasons", "llm.finish_reason", "ai.response.finishReason")
RESPONSE_ID = ("gen_ai.response.id", "ai.response.id")

USAGE = {
    # OpenTelemetry GenAI (current)
    "gen_ai.usage.input_tokens": "input_tokens",
    "gen_ai.usage.output_tokens": "output_tokens",
    "gen_ai.usage.cache_read.input_tokens": "cached_input_tokens",
    "gen_ai.usage.cache_write.input_tokens": "cache_write_tokens",
    "gen_ai.usage.cache_creation.input_tokens": "cache_write_tokens",
    "gen_ai.usage.audio.input_tokens": "input_audio_tokens",
    "gen_ai.usage.audio.output_tokens": "output_audio_tokens",
    "gen_ai.usage.reasoning.output_tokens": "reasoning_tokens",
    # Older GenAI names and OpenLLMetry
    "gen_ai.usage.prompt_tokens": "input_tokens",
    "gen_ai.usage.completion_tokens": "output_tokens",
    "gen_ai.usage.cache_read_input_tokens": "cached_input_tokens",
    "gen_ai.usage.cache_creation_input_tokens": "cache_write_tokens",
    "gen_ai.usage.reasoning_tokens": "reasoning_tokens",
    "llm.usage.reasoning_tokens": "reasoning_tokens",
    # OpenInference
    "llm.token_count.prompt": "input_tokens",
    "llm.token_count.completion": "output_tokens",
    "llm.token_count.prompt_details.cache_read": "cached_input_tokens",
    "llm.token_count.prompt_details.cache_write": "cache_write_tokens",
    "llm.token_count.prompt_details.audio": "input_audio_tokens",
    "llm.token_count.completion_details.audio": "output_audio_tokens",
    "llm.token_count.completion_details.reasoning": "reasoning_tokens",
    # Vercel AI SDK (v4 and v5 names)
    "ai.usage.promptTokens": "input_tokens",
    "ai.usage.completionTokens": "output_tokens",
    "ai.usage.inputTokens": "input_tokens",
    "ai.usage.outputTokens": "output_tokens",
    "ai.usage.cachedInputTokens": "cached_input_tokens",
    "ai.usage.reasoningTokens": "reasoning_tokens",
    "ai.usage.tokens": "input_tokens",  # embeddings
}
# Totals are derivable; drop them rather than keep a duplicate attribute.
USAGE_TOTALS = (
    "gen_ai.usage.total_tokens",
    "llm.usage.total_tokens",
    "llm.token_count.total",
    "ai.usage.totalTokens",
)

PARAMS = {
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
    "llm.top_p": "top_p",
    "llm.temperature": "temperature",
    "ai.settings.temperature": "temperature",
    "ai.settings.maxTokens": "max_tokens",
    "ai.settings.maxOutputTokens": "max_tokens",
    "ai.settings.topP": "top_p",
    "ai.settings.topK": "top_k",
    "ai.settings.frequencyPenalty": "frequency_penalty",
    "ai.settings.presencePenalty": "presence_penalty",
    "ai.settings.seed": "seed",
    "ai.settings.stopSequences": "stop",
    "ai.settings.maxRetries": "max_retries",
}

# Keys that only describe the payload format.
NOISE = ("input.mime_type", "output.mime_type", "ai.operationId", "operation.name")


# -- helpers ----------------------------------------------------------------------------------


def maybe_json(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _any_json(value: Any) -> Any:
    """OpenLLMetry JSON-encodes entity input and output, scalars included."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _first(a: dict[str, Any], keys: tuple[str, ...]) -> Any:
    found = None
    for k in keys:
        if k in a:
            v = a.pop(k)
            if found is None and v not in (None, ""):
                found = v
    return found


_SEG = re.compile(r"\.(\d+)(?=\.|$)")


def unflatten(a: dict[str, Any], prefix: str) -> list[Any] | None:
    """Pop ``prefix.0.x.y`` style keys and rebuild them as a list of nested dicts.

    ``llm.input_messages.0.message.role = "user"`` becomes ``[{"message": {"role": "user"}}]``.
    """
    keys = [k for k in a if k.startswith(prefix + ".")]
    if not keys:
        return None
    root: dict[str, Any] = {}
    for key in keys:
        value = a.pop(key)
        parts = key[len(prefix) + 1 :].split(".")
        node: Any = root
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last:
                node[part] = maybe_json(value) if isinstance(value, str) else value
            else:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):  # a scalar and a subtree under one key
                    break
    return _listify(root)  # type: ignore[no-any-return]


def _listify(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    out = {k: _listify(v) for k, v in node.items()}
    if out and all(k.isdigit() for k in out):
        return [out[k] for k in sorted(out, key=int)]
    return out


_WRAPPERS = ("message", "message_content", "document", "tool_call", "tool", "function")


def unwrap(node: Any) -> Any:
    """Drop OpenInference's single-key wrappers (``{"message": {...}}`` -> ``{...}``) and
    rename ``contents`` to ``content``."""
    if isinstance(node, list):
        return [unwrap(v) for v in node]
    if not isinstance(node, dict):
        return node
    if len(node) == 1:
        ((key, value),) = node.items()
        if key in _WRAPPERS and isinstance(value, dict):
            return unwrap(value)
    out = {}
    for k, v in node.items():
        out["content" if k == "contents" else k] = unwrap(v)
    return out


def _b64(text: str) -> bytes | None:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None


def blobs_to_data_uris(node: Any) -> Any:
    """Rewrite inline base64 media parts as ``data:`` URIs so media extraction finds them.

    Shapes: GenAI ``{"type": "blob", "mime_type", "content": b64}``; Vercel AI SDK
    ``{"type": "image", "image": b64, "mimeType"}`` and ``{"type": "file", "data": b64,
    "mediaType"|"mimeType"}``. Strings that are not valid base64 are left alone.
    """
    if isinstance(node, list):
        return [blobs_to_data_uris(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: blobs_to_data_uris(v) for k, v in node.items()}
    kind = node.get("type")
    for type_, data_key in (("blob", "content"), ("image", "image"), ("file", "data")):
        if kind != type_ or not isinstance(node.get(data_key), str):
            continue
        raw = node[data_key]
        if raw.startswith(("data:", "http://", "https://")) or _b64(raw) is None:
            continue
        mime = (
            node.get("mime_type")
            or node.get("mimeType")
            or node.get("mediaType")
            or ("image/*" if type_ == "image" else "application/octet-stream")
        )
        out[data_key] = f"data:{mime};base64,{raw}"
    return out


# -- the mapping ------------------------------------------------------------------------------


def _kind(a: dict[str, Any], name: str) -> str:
    own = a.pop("tirkit.span.kind", None)  # written by spans_to_otlp
    if own:
        return str(own)
    op = a.pop("gen_ai.operation.name", None)
    oi = a.pop("openinference.span.kind", None)
    tl = a.pop("traceloop.span.kind", None)
    req_type = a.pop("llm.request.type", None)
    vercel = a.get("ai.operationId") or (name if name in VERCEL_KIND else None)
    db = str(a.get("db.system.name") or a.get("db.system") or "").lower()
    if "mcp.method.name" in a or a.get("gen_ai.tool.type") == "mcp":
        return "mcp_tool"
    for value, table in (
        (op, GENAI_OPERATION_KIND),
        (oi, OPENINFERENCE_KIND),
        (vercel, VERCEL_KIND),
        (req_type, TRACELOOP_REQUEST_TYPE),
        (tl, TRACELOOP_KIND),
    ):
        if value is not None and str(value) in table:
            return table[str(value)]
    if db in VECTOR_DBS or any(k.startswith("db.vector.") for k in a):
        return "vector_db"
    if any(k.startswith(("gen_ai.usage", "llm.token_count", "ai.usage")) for k in a):
        return "llm"
    return ""


def _messages_from_events(events: list[dict[str, Any]]) -> tuple[list[Any], list[Any]]:
    """GenAI span events: ``gen_ai.{system,user,assistant,tool}.message`` and ``gen_ai.choice``
    (one event per message) and the older ``gen_ai.content.prompt|completion``."""
    inputs: list[Any] = []
    outputs: list[Any] = []
    for ev in events:
        name = ev.get("name") or ""
        attrs = ev.get("attributes") or {}
        if name == "gen_ai.content.prompt" and "gen_ai.prompt" in attrs:
            value = maybe_json(attrs["gen_ai.prompt"])
            inputs.extend(value if isinstance(value, list) else [value])
        elif name == "gen_ai.content.completion" and "gen_ai.completion" in attrs:
            value = maybe_json(attrs["gen_ai.completion"])
            outputs.extend(value if isinstance(value, list) else [value])
        elif name.startswith("gen_ai.") and name.endswith(".message"):
            role = name[len("gen_ai.") : -len(".message")]
            msg = {"role": attrs.get("role", role)}
            for k, v in attrs.items():
                if k not in ("gen_ai.system", "gen_ai.provider.name", "role"):
                    msg[k] = maybe_json(v)
            inputs.append(msg)
        elif name == "gen_ai.choice":
            outputs.append(
                {
                    k: maybe_json(v)
                    for k, v in attrs.items()
                    if k not in ("gen_ai.system", "gen_ai.provider.name")
                }
            )
    return inputs, outputs


def _io(a: dict[str, Any], events: list[dict[str, Any]], s: Span) -> None:
    inp: dict[str, Any] = {}
    out: Any = None
    # OpenTelemetry GenAI, attributes
    if "gen_ai.input.messages" in a:
        inp["messages"] = maybe_json(a.pop("gen_ai.input.messages"))
    if "gen_ai.system_instructions" in a:
        inp["system"] = maybe_json(a.pop("gen_ai.system_instructions"))
    if "gen_ai.tool.definitions" in a:
        inp["tools"] = maybe_json(a.pop("gen_ai.tool.definitions"))
    if "gen_ai.tool.call.arguments" in a:
        inp["arguments"] = maybe_json(a.pop("gen_ai.tool.call.arguments"))
    if "gen_ai.retrieval.query.text" in a:
        inp["query"] = a.pop("gen_ai.retrieval.query.text")
    if "gen_ai.output.messages" in a:
        out = maybe_json(a.pop("gen_ai.output.messages"))
    elif "gen_ai.tool.call.result" in a:
        out = maybe_json(a.pop("gen_ai.tool.call.result"))
    elif "gen_ai.retrieval.documents" in a:
        out = maybe_json(a.pop("gen_ai.retrieval.documents"))
    # Older GenAI / OpenLLMetry indexed attributes
    prompt = unflatten(a, "gen_ai.prompt")
    completion = unflatten(a, "gen_ai.completion")
    if prompt and "messages" not in inp:
        inp["messages"] = prompt
    if completion and out is None:
        out = completion
    # OpenInference indexed attributes
    oi_in = unflatten(a, "llm.input_messages")
    oi_out = unflatten(a, "llm.output_messages")
    oi_tools = unflatten(a, "llm.tools")
    oi_docs = unflatten(a, "retrieval.documents")
    rr_in = unflatten(a, "reranker.input_documents")
    rr_out = unflatten(a, "reranker.output_documents")
    if oi_in and "messages" not in inp:
        inp["messages"] = unwrap(oi_in)
    if oi_tools and "tools" not in inp:
        inp["tools"] = unwrap(oi_tools)
    if oi_out and out is None:
        out = unwrap(oi_out)
    if oi_docs and out is None:
        out = unwrap(oi_docs)
    if "reranker.query" in a:
        inp["query"] = a.pop("reranker.query")
    if rr_in:
        inp["documents"] = unwrap(rr_in)
    if rr_out and out is None:
        out = unwrap(rr_out)
    # Vercel AI SDK
    if "ai.prompt.messages" in a and "messages" not in inp:
        inp["messages"] = maybe_json(a.pop("ai.prompt.messages"))
    elif "ai.prompt" in a and not inp:
        prompt_obj = maybe_json(a.pop("ai.prompt"))
        if isinstance(prompt_obj, dict):
            inp.update(prompt_obj)
        else:
            inp["prompt"] = prompt_obj
    if "ai.prompt.tools" in a and "tools" not in inp:
        tools = a.pop("ai.prompt.tools")
        inp["tools"] = [maybe_json(t) for t in tools] if isinstance(tools, list) else tools
    if "ai.toolCall.args" in a:
        inp["arguments"] = maybe_json(a.pop("ai.toolCall.args"))
    if "ai.value" in a and not inp:
        inp["input"] = maybe_json(a.pop("ai.value"))
    if "ai.values" in a and not inp:
        values = a.pop("ai.values")
        inp["input"] = [maybe_json(v) for v in values] if isinstance(values, list) else values
    vercel_out: dict[str, Any] = {}
    for key, field in (
        ("ai.response.text", "text"),
        ("ai.response.object", "object"),
        ("ai.response.toolCalls", "tool_calls"),
        ("ai.result.text", "text"),
        ("ai.result.object", "object"),
        ("ai.result.toolCalls", "tool_calls"),
    ):
        if key in a:
            vercel_out.setdefault(field, maybe_json(a.pop(key)))
    if "ai.toolCall.result" in a and out is None:
        out = maybe_json(a.pop("ai.toolCall.result"))
    if vercel_out and out is None:
        out = vercel_out["text"] if list(vercel_out) == ["text"] else vercel_out
    a.pop("ai.embedding", None)
    a.pop("ai.embeddings", None)
    # Span events (GenAI conventions before content moved to attributes)
    ev_in, ev_out = _messages_from_events(events)
    if ev_in and "messages" not in inp:
        inp["messages"] = ev_in
    if ev_out and out is None:
        out = ev_out
    # Generic fallbacks: OpenInference and OpenLLMetry entity input and output
    generic_in = a.pop("input.value", None)
    generic_out = a.pop("output.value", None)
    entity_in = a.pop("traceloop.entity.input", None)
    entity_out = a.pop("traceloop.entity.output", None)
    if inp:
        s.input = inp
    elif generic_in is not None:
        s.input = maybe_json(generic_in)
    elif entity_in is not None:
        s.input = _any_json(entity_in)
    if out is None:
        out = maybe_json(generic_out) if generic_out is not None else _any_json(entity_out)
    s.output = out
    s.input = blobs_to_data_uris(s.input)
    s.output = blobs_to_data_uris(s.output)


def _metadata(a: dict[str, Any], s: Span) -> None:
    meta: dict[str, Any] = {}
    if "metadata" in a:
        value = maybe_json(a.pop("metadata"))
        if isinstance(value, dict):
            meta.update(value)
        else:
            meta["metadata"] = value
    for key in [k for k in a if k.startswith("ai.telemetry.metadata.")]:
        meta[key[len("ai.telemetry.metadata.") :]] = a.pop(key)
    for key in [k for k in a if k.startswith("traceloop.association.properties.")]:
        meta[key[len("traceloop.association.properties.") :]] = a.pop(key)
    if meta:
        s.attributes["metadata"] = meta


def apply(s: Span, a: dict[str, Any], events: list[dict[str, Any]] | None = None) -> None:
    """Fill ``s`` from OTLP span attributes ``a`` (decoded) and span events. Keys that were
    understood are removed from ``a``; the caller keeps the rest as attributes."""
    events = events or []
    s.kind = _kind(a, s.name) or "generic"
    model = _first(a, MODEL)
    if model is not None:
        s.model = str(model)
    provider = _first(a, PROVIDER)
    if provider:
        # Vercel: "openai.chat" -> "openai"
        s.attributes["provider"] = str(provider).split(".")[0]
    for key, unit in USAGE.items():
        if key in a:
            value = a.pop(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                s.usage.setdefault(unit, float(value))
    for key in USAGE_TOTALS:
        a.pop(key, None)
    cost = a.pop("tirkit.cost_usd", None)
    if isinstance(cost, int | float):
        s.cost_usd = float(cost)
    for key, param in PARAMS.items():
        if key in a:
            s.model_params.setdefault(param, a.pop(key))
    invocation = a.pop("llm.invocation_parameters", None)
    if invocation is not None:
        params = maybe_json(invocation)
        if isinstance(params, dict):
            for k, v in params.items():
                if k not in ("model", "messages", "tools", "input", "stream"):
                    s.model_params.setdefault(k, v)
    _io(a, events, s)
    tool = _first(a, TOOL_NAME)
    if tool is not None:
        s.attributes["tool_name"] = tool
    session = _first(a, SESSION)
    user = _first(a, USER)
    s.session_id = str(session) if session is not None else None
    s.user_id = str(user) if user is not None else None
    tags = _first(a, TAGS)
    if tags:
        tags = maybe_json(tags)
        s.tags = [str(t) for t in (tags if isinstance(tags, list) else [tags])]
    for key, factor in TTFT:
        if key in a:
            value = a.pop(key)
            if isinstance(value, int | float) and s.ttft_ms is None:
                s.ttft_ms = float(value) * factor
    finish = _first(a, FINISH_REASON)
    if finish is not None:
        s.attributes["finish_reason"] = finish
    response_id = _first(a, RESPONSE_ID)
    if response_id is not None:
        s.attributes["response_id"] = response_id
    _metadata(a, s)
    for key in NOISE:
        a.pop(key, None)
    if s.status == "error" and not s.error:
        for ev in events:
            if ev.get("name") == "exception":
                ea = ev.get("attributes") or {}
                s.error = (
                    f"{ea.get('exception.type', 'Error')}: {ea.get('exception.message', '')}"
                ).strip()
                break
