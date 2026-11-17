"""LlamaIndex: a span handler and an event handler for LlamaIndex's instrumentation module.

Every instrumented LlamaIndex call (query engines, retrievers, synthesizers, agents, LLM and
embedding calls) becomes a Tirkit span nested like LlamaIndex nests them. Events add detail:
model name and messages, token usage, retrieved nodes and tool calls. Streamed LLM calls are
recorded when the call starts (LlamaIndex closes its span before the chunks arrive), so they
have no output, usage or time to first token.

    from tirkit.integrations.llamaindex import instrument
    instrument()          # or tirkit.instrument("llamaindex")

LlamaIndex keeps finished spans in its handler by default; this one does not, so a long-running
service does not grow in memory.
"""

from __future__ import annotations

import threading
from typing import Any

from tirkit.serialize import to_jsonable
from tirkit.tracing import Span, current_span, finish_span, start_span

_spans: dict[str, Span] = {}
_lock = threading.Lock()
_installed = False


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if obj is None:
            return None
        obj = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    return obj


def _name(id_: str) -> str:
    # ids look like "RetrieverQueryEngine.query-3f9c..." (qualified name, dash, uuid)
    return id_.rsplit("-", 5)[0] if id_.count("-") >= 5 else id_.split("-")[0]


def _usage(s: Span, response: Any) -> None:
    raw = _get(response, "raw")
    usage = _get(raw, "usage") or {}
    extra = _get(response, "additional_kwargs") or {}
    tin = _get(usage, "prompt_tokens") or _get(usage, "input_tokens") or extra.get("prompt_tokens")
    tout = (
        _get(usage, "completion_tokens")
        or _get(usage, "output_tokens")
        or extra.get("completion_tokens")
    )
    cached = _get(usage, "prompt_tokens_details", "cached_tokens")
    s.set_usage(input_tokens=tin, output_tokens=tout, cached_input_tokens=cached or None)


def _node(n: Any) -> dict[str, Any]:
    node = _get(n, "node") or n
    return {
        "content": (node.get_content() if hasattr(node, "get_content") else str(node))[:5000],
        "score": _get(n, "score"),
        "id": _get(node, "node_id"),
        "metadata": to_jsonable(_get(node, "metadata") or {}),
    }


def _handle(event: Any) -> None:
    """Fill the Tirkit span of ``event.span_id`` from a LlamaIndex event."""
    with _lock:
        s = _spans.get(event.span_id)
    if s is None:
        return
    kind = type(event).__name__
    if kind == "LLMChatStartEvent":
        s.kind = "llm"
        s.input = {"messages": to_jsonable(event.messages)}
        s.model = _get(event.model_dict, "model") or _get(event.model_dict, "model_name") or s.model
        keep = ("temperature", "max_tokens", "top_p", "seed")
        s.model_params = {k: v for k, v in (event.model_dict or {}).items() if k in keep}
    elif kind == "LLMCompletionStartEvent":
        s.kind = "llm"
        s.input = {"prompt": event.prompt}
        s.model = _get(event.model_dict, "model") or _get(event.model_dict, "model_name") or s.model
    elif kind in ("LLMChatEndEvent", "LLMCompletionEndEvent"):
        s.output = to_jsonable(_get(event.response, "message") or _get(event.response, "text"))
        _usage(s, event.response)
    elif kind == "EmbeddingStartEvent":
        s.kind = "embedding"
        s.model = _get(event.model_dict, "model_name") or s.model
    elif kind == "EmbeddingEndEvent":
        s.input = {"chunks": list(event.chunks)}
        s.output = {"embeddings": len(event.embeddings)}
    elif kind == "RetrievalStartEvent":
        s.kind = "retrieval"
        q = event.str_or_query_bundle
        s.input = {"query": q if isinstance(q, str) else _get(q, "query_str")}
    elif kind == "RetrievalEndEvent":
        s.kind = "retrieval"
        s.output = [_node(n) for n in event.nodes]
    elif kind == "ReRankStartEvent":
        s.kind = "rerank"
        s.input = {"query": str(event.query), "documents": [_node(n) for n in event.nodes]}
        s.model = event.model_name or s.model
        s.attributes["top_n"] = event.top_n
    elif kind == "ReRankEndEvent":
        s.output = [_node(n) for n in event.nodes]
    elif kind == "AgentToolCallEvent":
        s.kind = "tool"
        s.name = _get(event.tool, "name") or s.name
        s.input = to_jsonable(event.arguments)


def _build() -> tuple[Any, Any]:
    from llama_index.core.instrumentation.event_handlers import BaseEventHandler
    from llama_index.core.instrumentation.span import BaseSpan
    from llama_index.core.instrumentation.span_handlers import BaseSpanHandler

    class _Ref(BaseSpan):
        pass

    class TirkitSpanHandler(BaseSpanHandler[_Ref]):  # type: ignore[misc]
        @classmethod
        def class_name(cls) -> str:
            return "TirkitSpanHandler"

        def new_span(
            self,
            id_: str,
            bound_args: Any,
            instance: Any = None,
            parent_span_id: str | None = None,
            tags: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> _Ref | None:
            try:
                with _lock:
                    parent = _spans.get(parent_span_id) if parent_span_id else None
                args = {k: v for k, v in bound_args.arguments.items() if k not in ("self", "cls")}
                s = start_span(
                    _name(id_),
                    parent=parent or current_span(),
                    kind="chain",
                    input=args or None,
                )
                with _lock:
                    _spans[id_] = s
            except Exception:  # noqa: BLE001 - tracing must never break the caller
                return None
            return _Ref(id_=id_, parent_id=parent_span_id, tags=tags or {})

        def _close(self, id_: str, result: Any, error: BaseException | None) -> _Ref | None:
            with _lock:
                s = _spans.pop(id_, None)
            ref = self.open_spans.get(id_)
            if s is None:
                return ref
            try:
                if error is not None:
                    s.record_error(error)
                elif s.output is None and result is not None:
                    s.output = to_jsonable(_response(result))
            finally:
                finish_span(s)
            return ref

        def prepare_to_exit_span(
            self,
            id_: str,
            bound_args: Any,
            instance: Any = None,
            result: Any = None,
            **kwargs: Any,
        ) -> _Ref | None:
            return self._close(id_, result, None)

        def prepare_to_drop_span(
            self,
            id_: str,
            bound_args: Any,
            instance: Any = None,
            err: BaseException | None = None,
            **kwargs: Any,
        ) -> _Ref | None:
            return self._close(id_, None, err or RuntimeError("error"))

    class TirkitEventHandler(BaseEventHandler):  # type: ignore[misc]
        @classmethod
        def class_name(cls) -> str:
            return "TirkitEventHandler"

        def handle(self, event: Any, **kwargs: Any) -> None:
            try:
                _handle(event)
            except Exception:  # noqa: BLE001
                pass

    return TirkitSpanHandler, TirkitEventHandler


def _response(result: Any) -> Any:
    """LlamaIndex results (Response, ChatResponse...) as text or plain data."""
    for attr in ("response", "text", "message"):
        value = getattr(result, attr, None)
        if value is not None and not callable(value):
            return value
    return result


def instrument() -> bool:
    """Attach the handlers to LlamaIndex's root dispatcher (once)."""
    global _installed
    try:
        import llama_index.core.instrumentation as instrumentation
    except ImportError:
        return False
    if _installed:
        return True
    span_handler, event_handler = _build()
    dispatcher = instrumentation.get_dispatcher()
    dispatcher.add_span_handler(span_handler())
    dispatcher.add_event_handler(event_handler())
    _installed = True
    return True
