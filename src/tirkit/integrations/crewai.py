"""CrewAI: a listener on CrewAI's event bus that records crews, tasks, agents, tool calls and
model calls as Tirkit spans.

    from tirkit.integrations.crewai import instrument
    instrument()         # or tirkit.instrument("crewai")

CrewAI delivers events on a thread pool, so a "completed" event can be handled before its
"started" event. Each span is built when both events are known and its parent's start event has
been seen, so spans nest correctly whatever order the handlers ran in. Spans share one trace per
crew run (the kickoff event's id).
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from tirkit.serialize import to_jsonable
from tirkit.tracing import Span, finish_span

_MAX_TRACES = 10_000

# started event class -> (span kind, name, input)
_START: dict[str, Callable[[Any], tuple[str, str, Any]]] = {
    "CrewKickoffStartedEvent": lambda e: (
        "chain",
        getattr(e, "crew_name", None) or "crew",
        to_jsonable(getattr(e, "inputs", None)),
    ),
    "TaskStartedEvent": lambda e: (
        "chain",
        getattr(e, "task_name", None)
        or str(getattr(getattr(e, "task", None), "description", "task"))[:80],
        to_jsonable(getattr(e, "context", None)),
    ),
    "AgentExecutionStartedEvent": lambda e: (
        "agent",
        getattr(e, "agent_role", None) or "agent",
        {"task": getattr(e, "task_prompt", None)},
    ),
    "ToolUsageStartedEvent": lambda e: (
        "tool",
        getattr(e, "tool_name", None) or "tool",
        to_jsonable(getattr(e, "tool_args", None)),
    ),
    "LLMCallStartedEvent": lambda e: (
        "llm",
        f"llm {getattr(e, 'model', '') or ''}".strip(),
        {"messages": to_jsonable(getattr(e, "messages", None))},
    ),
}
# end event class -> (which output attribute, is it an error)
_END: dict[str, tuple[str, bool]] = {
    "CrewKickoffCompletedEvent": ("output", False),
    "TaskCompletedEvent": ("output", False),
    "AgentExecutionCompletedEvent": ("output", False),
    "ToolUsageFinishedEvent": ("output", False),
    "LLMCallCompletedEvent": ("response", False),
    "CrewKickoffFailedEvent": ("error", True),
    "TaskFailedEvent": ("error", True),
    "AgentExecutionErrorEvent": ("error", True),
    "ToolUsageErrorEvent": ("error", True),
    "LLMCallFailedEvent": ("error", True),
}


def _ts(event: Any) -> float | None:
    stamp: Any = getattr(event, "timestamp", None)
    return float(stamp.timestamp()) if hasattr(stamp, "timestamp") else None


def _hex(event_id: str, n: int) -> str:
    return uuid.UUID(str(event_id)).hex[:n]


class CrewTracer:
    """Collects CrewAI events and turns matched start/end pairs into Tirkit spans."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._starts: dict[str, Any] = {}
        self._ends: dict[str, Any] = {}
        self._traces: OrderedDict[str, str] = OrderedDict()
        self._waiting: dict[str, list[str]] = {}  # parent event id -> children's start ids

    # entry points ------------------------------------------------------------------------------

    def on_start(self, event: Any) -> None:
        eid = str(event.event_id)
        with self._lock:
            self._starts[eid] = event
            done = self._resolve_locked(eid)
        self._emit(done)

    def on_end(self, event: Any) -> None:
        key = str(getattr(event, "started_event_id", None) or "")
        if not key:
            return
        with self._lock:
            self._ends[key] = event
            done = self._finalize_locked(key)
        self._emit(done)

    # internals ---------------------------------------------------------------------------------

    def _resolve_locked(self, eid: str) -> list[tuple[Any, Any, str]]:
        """Give ``eid`` a trace id if its parent is known, then release children waiting on it."""
        event = self._starts[eid]
        parent = str(event.parent_event_id) if getattr(event, "parent_event_id", None) else None
        if parent is None:
            trace = _hex(eid, 32)
        elif parent in self._traces:
            trace = self._traces[parent]
        else:
            self._waiting.setdefault(parent, []).append(eid)
            return []
        self._traces[eid] = trace
        while len(self._traces) > _MAX_TRACES:
            self._traces.popitem(last=False)
        done = self._finalize_locked(eid)
        for child in self._waiting.pop(eid, []):
            done.extend(self._resolve_locked(child))
        return done

    def _finalize_locked(self, eid: str) -> list[tuple[Any, Any, str]]:
        if eid in self._starts and eid in self._ends and eid in self._traces:
            return [(self._starts.pop(eid), self._ends.pop(eid), self._traces[eid])]
        return []

    def _emit(self, done: list[tuple[Any, Any, str]]) -> None:
        for start, end, trace in done:
            try:
                finish_span(self._build(start, end, trace))
            except Exception:  # noqa: BLE001 - tracing must never break the crew
                pass

    @staticmethod
    def _build(start: Any, end: Any, trace: str) -> Span:
        kind, name, inp = _START[type(start).__name__](start)
        s = Span(name=name, kind=kind, trace_id=trace, span_id=_hex(start.event_id, 16), input=inp)
        if getattr(start, "parent_event_id", None):
            s.parent_id = _hex(start.parent_event_id, 16)
        t0, t1 = _ts(start), _ts(end)
        if t0 is not None:
            s.start = t0
        s.end = t1 if t1 is not None else s.start
        if kind == "llm":
            s.model = getattr(start, "model", None)
            keep = ("temperature", "top_p", "max_tokens", "seed", "n", "stop_sequences")
            s.model_params = {
                k: getattr(start, k) for k in keep if getattr(start, k, None) is not None
            }
            s.attributes["provider"] = "crewai"
        attr, is_error = _END[type(end).__name__]
        value = getattr(end, attr, None)
        if is_error:
            s.record_error(str(value) if value else "error")
        else:
            s.output = to_jsonable(value)
        if kind == "llm":
            usage = getattr(end, "usage", None) or {}
            s.set_usage(
                input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
                output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
                cached_input_tokens=usage.get("cached_prompt_tokens") or None,
            )
            if getattr(end, "finish_reason", None):
                s.attributes["finish_reason"] = end.finish_reason
        if type(end).__name__ == "CrewKickoffCompletedEvent" and getattr(end, "total_tokens", None):
            s.attributes["total_tokens"] = end.total_tokens
        return s


_tracer: CrewTracer | None = None


def instrument() -> bool:
    """Subscribe to CrewAI's event bus (once). Returns False when CrewAI is not installed."""
    global _tracer
    try:
        import crewai.events as events
        from crewai.events import crewai_event_bus
    except ImportError:
        return False
    if _tracer is not None:
        return True
    tracer = _tracer = CrewTracer()
    for name in _START:
        cls = getattr(events, name, None)
        if cls is not None:
            crewai_event_bus.on(cls)(lambda source, event: tracer.on_start(event))
    for name in _END:
        cls = getattr(events, name, None)
        if cls is not None:
            crewai_event_bus.on(cls)(lambda source, event: tracer.on_end(event))
    return True
