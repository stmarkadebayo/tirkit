"""OpenAI Agents SDK: a tracing processor that records traces, agents, model calls, tool calls,
handoffs and guardrails as Tirkit spans.

    from tirkit.integrations.openai_agents import instrument
    instrument()                      # keeps the SDK's own exporter
    instrument(replace_default=True)  # send traces to Tirkit only

or ``tirkit.instrument("openai_agents")``. Works with the SDK's span data types, so it needs
no network and no extra configuration.
"""

from __future__ import annotations

import threading
from typing import Any

from tirkit.serialize import to_jsonable
from tirkit.tracing import Span, current_span, finish_span, start_span


def _bare(sdk_id: str | None) -> str | None:
    """``trace_ab12...`` / ``span_cd34...`` -> the hex part, like OTLP ids."""
    return sdk_id.split("_", 1)[1] if sdk_id and "_" in sdk_id else sdk_id


def _usage(s: Span, usage: Any) -> None:
    if not usage:
        return
    get = (
        (lambda k: usage.get(k)) if isinstance(usage, dict) else (lambda k: getattr(usage, k, None))
    )
    details_in = get("input_tokens_details")
    details_out = get("output_tokens_details")
    cached = (
        details_in.get("cached_tokens")
        if isinstance(details_in, dict)
        else getattr(details_in, "cached_tokens", None)
    )
    reasoning = (
        details_out.get("reasoning_tokens")
        if isinstance(details_out, dict)
        else getattr(details_out, "reasoning_tokens", None)
    )
    s.set_usage(
        input_tokens=get("input_tokens") or get("prompt_tokens"),
        output_tokens=get("output_tokens") or get("completion_tokens"),
        cached_input_tokens=cached or None,
        reasoning_tokens=reasoning or None,
    )


def fill(s: Span, data: Any) -> None:
    """Set ``s`` from an Agents SDK span-data object."""
    kind = type(data).__name__
    if kind == "AgentSpanData":
        s.kind = "agent"
        s.name = data.name
        s.attributes.update(
            {k: v for k, v in (("handoffs", data.handoffs), ("tools", data.tools)) if v}
        )
        if getattr(data, "output_type", None):
            s.attributes["output_type"] = data.output_type
    elif kind == "GenerationSpanData":
        s.kind, s.name = "llm", "generation"
        s.model = data.model
        s.model_params = to_jsonable(data.model_config or {})
        s.input, s.output = {"messages": to_jsonable(data.input)}, to_jsonable(data.output)
        _usage(s, data.usage)
    elif kind == "ResponseSpanData":
        s.kind, s.name = "llm", "response"
        resp = data.response
        s.model = getattr(resp, "model", None)
        s.input = {"input": to_jsonable(getattr(data, "input", None))}
        out = getattr(resp, "output", None)
        s.output = to_jsonable(out) if out is not None else None
        s.attributes["response_id"] = getattr(resp, "id", None)
        _usage(s, getattr(resp, "usage", None) or data.usage)
    elif kind == "FunctionSpanData":
        s.kind = "mcp_tool" if getattr(data, "mcp_data", None) else "tool"
        s.name = data.name
        s.input, s.output = to_jsonable(data.input), to_jsonable(data.output)
        if getattr(data, "mcp_data", None):
            s.attributes["mcp"] = to_jsonable(data.mcp_data)
    elif kind == "HandoffSpanData":
        s.name = f"handoff: {data.from_agent} -> {data.to_agent}"
        s.attributes.update(from_agent=data.from_agent, to_agent=data.to_agent)
    elif kind == "GuardrailSpanData":
        s.kind, s.name = "guardrail", data.name
        s.output = {"triggered": data.triggered}
        if data.triggered:
            s.level = "warning"
    elif kind == "MCPListToolsSpanData":
        s.kind, s.name = "mcp_tool", f"mcp list tools: {data.server}"
        s.output = to_jsonable(data.result)
    elif kind == "CustomSpanData":
        s.name = data.name
        s.output = to_jsonable(data.data)
    else:  # speech, transcription, turn, task: keep what the SDK exports
        exported = to_jsonable(data.export()) if hasattr(data, "export") else {}
        s.name = str(exported.get("type") or kind)
        s.attributes["data"] = exported


class TirkitTracingProcessor:
    """Implements the Agents SDK ``TracingProcessor`` interface (duck typed, so importing this
    module does not import the SDK)."""

    def __init__(self) -> None:
        self._spans: dict[str, Span] = {}
        self._lock = threading.Lock()

    def on_trace_start(self, trace: Any) -> None:
        s = start_span(
            trace.name,
            parent=current_span(),
            kind="chain",
            workflow=trace.name,
        )
        s.trace_id = _bare(trace.trace_id) or s.trace_id
        if getattr(trace, "group_id", None):
            s.session_id = trace.group_id
        if getattr(trace, "metadata", None):
            s.attributes["metadata"] = to_jsonable(trace.metadata)
        with self._lock:
            self._spans[trace.trace_id] = s

    def on_trace_end(self, trace: Any) -> None:
        with self._lock:
            s = self._spans.pop(trace.trace_id, None)
        if s is not None:
            finish_span(s)

    def on_span_start(self, span: Any) -> None:
        with self._lock:
            parent = self._spans.get(span.parent_id) or self._spans.get(span.trace_id)
        s = start_span("span", parent=parent, kind="generic")
        s.trace_id = _bare(span.trace_id) or s.trace_id
        s.span_id = _bare(span.span_id) or s.span_id
        s.parent_id = (
            _bare(span.parent_id) if span.parent_id else (parent.span_id if parent else None)
        )
        if parent is not None and parent.span_id and not span.parent_id:
            s.parent_id = parent.span_id
        fill(s, span.span_data)  # names the span early; refreshed at the end with results
        with self._lock:
            self._spans[span.span_id] = s

    def on_span_end(self, span: Any) -> None:
        with self._lock:
            s = self._spans.pop(span.span_id, None)
        if s is None:
            return
        try:
            fill(s, span.span_data)
            if span.error:
                s.record_error(span.error.get("message") or "error")
        finally:
            finish_span(s)

    def shutdown(self) -> None:
        with self._lock:
            leftover, self._spans = list(self._spans.values()), {}
        for s in leftover:
            finish_span(s)

    def force_flush(self) -> None:
        return None


_installed: TirkitTracingProcessor | None = None


def instrument(*, replace_default: bool = False) -> bool:
    """Register the processor with the Agents SDK. Returns False when it is not installed."""
    try:
        import agents
    except ImportError:
        return False
    global _installed
    if _installed is not None and not replace_default:
        return True  # already registered: calling instrument() twice must not double-trace
    processor: Any = TirkitTracingProcessor()
    _installed = processor
    if replace_default:
        agents.set_trace_processors([processor])
    else:
        agents.add_trace_processor(processor)
    return True
