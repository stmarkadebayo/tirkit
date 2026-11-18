"""Auto-tracing for the Cohere Python SDK (v2 client): ``chat``, ``chat_stream``, ``embed`` and
``rerank``, sync and async. Rerank is billed per search, recorded as the ``requests`` unit."""

from __future__ import annotations

from typing import Any

from tirkit.instrumentation._patch import CallSpec, get, patch, wrap_method
from tirkit.serialize import to_jsonable
from tirkit.tracing import Span

_CHAT_EXCLUDE = {"model", "messages", "tools", "documents", "request_options"}


def _chat_input(kw: dict[str, Any]) -> Any:
    out: dict[str, Any] = {"messages": to_jsonable(kw.get("messages"))}
    for k in ("tools", "documents"):
        if kw.get(k):
            out[k] = to_jsonable(kw[k])
    return out


def _usage(s: Span, usage: Any) -> None:
    """Cohere reports ``tokens`` (what the model processed) and ``billed_units``; cost follows
    the billed units when present, as that is what Cohere charges for."""
    if usage is None:
        return
    src = get(usage, "billed_units") or get(usage, "tokens")
    s.set_usage(
        input_tokens=get(src, "input_tokens"),
        output_tokens=get(src, "output_tokens"),
        cached_input_tokens=get(src, "cached_tokens"),
    )
    if get(usage, "tokens", "input_tokens") is not None:
        s.attributes["tokens"] = to_jsonable(get(usage, "tokens"))


def _chat_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    s.output = to_jsonable(get(res, "message"))
    s.attributes["finish_reason"] = str(get(res, "finish_reason"))
    s.attributes["response_id"] = get(res, "id")
    _usage(s, get(res, "usage"))


def _chat_stream(s: Span, kw: dict[str, Any]) -> Any:
    state: dict[str, Any] = {"text": "", "thinking": "", "tool_calls": {}}

    def on_item(event: Any) -> None:
        etype = str(get(event, "type"))
        if etype == "message-start":
            s.attributes["response_id"] = get(event, "id")
        elif etype == "content-delta":
            content = get(event, "delta", "message", "content")
            state["text"] += get(content, "text") or ""
            state["thinking"] += get(content, "thinking") or ""
        elif etype == "tool-call-start":
            call = get(event, "delta", "message", "tool_calls")
            state["tool_calls"][get(event, "index") or 0] = {
                "id": get(call, "id"),
                "name": get(call, "function", "name"),
                "arguments": get(call, "function", "arguments") or "",
            }
        elif etype == "tool-call-delta":
            call = get(event, "delta", "message", "tool_calls")
            slot = state["tool_calls"].setdefault(
                get(event, "index") or 0, {"id": None, "name": None, "arguments": ""}
            )
            slot["arguments"] += get(call, "function", "arguments") or ""
        elif etype == "message-end":
            s.attributes["finish_reason"] = str(get(event, "delta", "finish_reason"))
            _usage(s, get(event, "delta", "usage"))

    def on_done() -> None:
        out: dict[str, Any] = {"role": "assistant", "content": state["text"]}
        if state["thinking"]:
            out["thinking"] = state["thinking"]
        if state["tool_calls"]:
            out["tool_calls"] = [v for _, v in sorted(state["tool_calls"].items())]
        s.output = out

    return on_item, on_done


def _embed_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    embeddings = get(res, "embeddings")
    first: list[Any] = (
        next(iter(to_jsonable(embeddings).values()), []) if embeddings is not None else []
    )
    s.output = {"embeddings": len(first)}
    s.set_usage(input_tokens=get(res, "meta", "billed_units", "input_tokens"))


def _rerank_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    s.output = [
        {"index": get(r, "index"), "score": get(r, "relevance_score")}
        for r in get(res, "results") or []
    ]
    s.set_usage(requests=get(res, "meta", "billed_units", "search_units") or 1)


def instrument() -> bool:
    try:
        from cohere.v2.client import AsyncV2Client, V2Client
    except ImportError:
        return False
    attrs = lambda client, kw: {"provider": "cohere"}  # noqa: E731
    chat = CallSpec(
        name="cohere.chat",
        kind=lambda kw: "llm",
        build_input=_chat_input,
        param_exclude=_CHAT_EXCLUDE,
        on_result=_chat_result,
        attributes=attrs,
    )
    stream = CallSpec(
        name="cohere.chat",
        kind=lambda kw: "llm",
        build_input=_chat_input,
        param_exclude=_CHAT_EXCLUDE,
        is_stream=lambda kw, res: True,
        stream_handlers=_chat_stream,
        attributes=attrs,
    )
    embed = CallSpec(
        name="cohere.embed",
        kind=lambda kw: "embedding",
        build_input=lambda kw: {"texts": kw.get("texts"), "input_type": kw.get("input_type")},
        param_exclude={"model", "texts", "images", "inputs", "request_options"},
        on_result=_embed_result,
        attributes=attrs,
    )
    rerank = CallSpec(
        name="cohere.rerank",
        kind=lambda kw: "rerank",
        build_input=lambda kw: {"query": kw.get("query"), "documents": kw.get("documents")},
        param_exclude={"model", "query", "documents", "request_options"},
        on_result=_rerank_result,
        attributes=attrs,
    )
    for owner in (V2Client, AsyncV2Client):
        patch(owner, "chat", wrap_method(chat))
        patch(owner, "chat_stream", wrap_method(stream))
        patch(owner, "embed", wrap_method(embed))
        patch(owner, "rerank", wrap_method(rerank))
    return True
