"""Auto-tracing for the Mistral Python SDK: ``chat.complete`` / ``chat.stream`` (sync and async)
and ``embeddings.create``. Responses and stream chunks have the OpenAI chat shape, so they are
read with the OpenAI helpers."""

from __future__ import annotations

import importlib
from typing import Any

from tirkit.instrumentation._patch import CallSpec, get, patch, wrap_method
from tirkit.instrumentation.openai import _chat_result, _chat_stream
from tirkit.tracing import Span

_EXCLUDE = {"messages", "model", "tools"}


def _input(kw: dict[str, Any]) -> Any:
    out: dict[str, Any] = {"messages": kw.get("messages")}
    if kw.get("tools"):
        out["tools"] = kw["tools"]
    return out


def _params(kw: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in kw.items()
        if k not in _EXCLUDE | {"retries", "server_url", "timeout_ms", "http_headers", "stream"}
        and v is not None
        and type(v).__name__ != "Unset"
    }


def _stream(s: Span, kw: dict[str, Any]) -> Any:
    on_item, on_done = _chat_stream(s, kw)
    # Stream events wrap the chunk: ``event.data``.
    return (lambda event: on_item(get(event, "data") or event)), on_done


def _embed_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    s.model = get(res, "model") or s.model
    s.output = {"embeddings": len(get(res, "data") or [])}
    s.set_usage(input_tokens=get(res, "usage", "prompt_tokens"))


def _load(*names: str) -> Any:
    for name in names:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def instrument() -> bool:
    # The SDK moved from ``mistralai.chat`` (v1) to ``mistralai.client.chat`` (v2).
    chat = _load("mistralai.client.chat", "mistralai.chat")
    if chat is None:
        return False
    attrs = lambda client, kw: {"provider": "mistral"}  # noqa: E731
    base = {
        "kind": lambda kw: "llm",
        "build_input": _input,
        "build_params": _params,
        "on_result": _chat_result,
        "attributes": attrs,
    }
    complete = CallSpec(name="mistral.chat", **base)  # type: ignore[arg-type]
    stream = CallSpec(
        name="mistral.chat",
        is_stream=lambda kw, res: True,
        stream_handlers=_stream,
        **base,  # type: ignore[arg-type]
    )
    for method, spec in (
        ("complete", complete),
        ("complete_async", complete),
        ("stream", stream),
        ("stream_async", stream),
    ):
        patch(chat.Chat, method, wrap_method(spec))
    embeddings = _load("mistralai.client.embeddings", "mistralai.embeddings")
    if embeddings is not None:
        embed = CallSpec(
            name="mistral.embeddings",
            kind=lambda kw: "embedding",
            build_input=lambda kw: {"input": kw.get("inputs", kw.get("input"))},
            param_exclude={"inputs", "input", "model"},
            on_result=_embed_result,
            attributes=attrs,
        )
        for method in ("create", "create_async"):
            patch(embeddings.Embeddings, method, wrap_method(embed))
    return True
