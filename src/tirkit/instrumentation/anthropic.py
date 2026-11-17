"""Auto-tracing for the Anthropic Python SDK: ``messages.create`` (incl. ``stream=True``) and the
``messages.stream()`` helper, sync and async. Records text, vision and document inputs, tool use,
thinking blocks, stop reasons, and token usage including prompt-cache reads and writes."""

from __future__ import annotations

import json
import time
from typing import Any

from tirkit.instrumentation._patch import CallSpec, get, params, patch, safe, wrap_method
from tirkit.tracing import Span, finish_span, start_span


def _input(kw: dict[str, Any]) -> Any:
    out: dict[str, Any] = {"messages": kw.get("messages")}
    for k in ("system", "tools"):
        v = kw.get(k)
        if v is not None and type(v).__name__ not in {"NotGiven", "Omit"}:
            out[k] = v
    return out


_EXCLUDE = {"messages", "model", "system", "tools"}


def _usage(s: Span, usage: Any) -> None:
    """Anthropic's ``input_tokens`` excludes cache reads/writes; Tirkit counts all input tokens."""
    if usage is None:
        return
    uncached = get(usage, "input_tokens") or 0
    cache_read = get(usage, "cache_read_input_tokens") or 0
    cache_write = get(usage, "cache_creation_input_tokens") or 0
    s.set_usage(
        input_tokens=uncached + cache_read + cache_write,
        cached_input_tokens=cache_read or None,
        cache_write_tokens=cache_write or None,
        output_tokens=get(usage, "output_tokens"),
    )


def _dump(obj: Any) -> Any:
    dump = getattr(obj, "model_dump", None)
    return dump(exclude_none=True) if callable(dump) else obj


def _result(s: Span, msg: Any, kw: dict[str, Any]) -> None:
    s.model = get(msg, "model") or s.model
    s.output = [_dump(b) for b in (get(msg, "content") or [])]
    s.attributes["stop_reason"] = get(msg, "stop_reason")
    s.attributes["response_id"] = get(msg, "id")
    if get(msg, "stop_reason") == "refusal":
        s.level = "warning"
        details = get(msg, "stop_details")
        if details is not None:
            s.attributes["stop_details"] = _dump(details)
    _usage(s, get(msg, "usage"))


def _raw_stream(s: Span, kw: dict[str, Any]) -> Any:
    """Rebuild the message from raw ``stream=True`` events."""
    blocks: dict[int, dict[str, Any]] = {}
    state: dict[str, Any] = {"usage": {}, "stop_reason": None}

    def on_item(event: Any) -> None:
        etype = get(event, "type")
        if etype == "message_start":
            msg = get(event, "message")
            s.model = get(msg, "model") or s.model
            s.attributes["response_id"] = get(msg, "id")
            u = get(msg, "usage")
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                if get(u, key) is not None:
                    state["usage"][key] = get(u, key)
        elif etype == "content_block_start":
            blocks[get(event, "index") or 0] = _dump(get(event, "content_block")) or {}
        elif etype == "content_block_delta":
            blk = blocks.setdefault(get(event, "index") or 0, {})
            delta = get(event, "delta")
            dtype = get(delta, "type")
            if dtype == "text_delta":
                blk["text"] = blk.get("text", "") + (get(delta, "text") or "")
            elif dtype == "input_json_delta":
                blk["_partial_json"] = blk.get("_partial_json", "") + (
                    get(delta, "partial_json") or ""
                )
            elif dtype == "thinking_delta":
                blk["thinking"] = blk.get("thinking", "") + (get(delta, "thinking") or "")
        elif etype == "message_delta":
            state["stop_reason"] = get(event, "delta", "stop_reason") or state["stop_reason"]
            u = get(event, "usage")
            if get(u, "output_tokens") is not None:
                state["usage"]["output_tokens"] = get(u, "output_tokens")

    def on_done() -> None:
        content = []
        for _, blk in sorted(blocks.items()):
            raw = blk.pop("_partial_json", None)
            if raw:
                try:
                    blk["input"] = json.loads(raw)
                except ValueError:
                    blk["input"] = raw
            content.append(blk)
        s.output = content
        s.attributes["stop_reason"] = state["stop_reason"]
        _usage(s, state["usage"])

    return on_item, on_done


class _StreamManagerProxy:
    """Wraps ``messages.stream(...)``: the span closes when the ``with`` block exits."""

    def __init__(self, manager: Any, span: Span, kw: dict[str, Any]) -> None:
        self._manager = manager
        self._span = span
        self._kw = kw
        self._stream: Any = None
        self._done = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    def _finish(self, exc: BaseException | None) -> None:
        if self._done:
            return
        self._done = True
        if exc is not None:
            self._span.record_error(exc)
        snapshot = safe(lambda: self._stream.current_message_snapshot) if self._stream else None
        if snapshot is not None:
            safe(_result, self._span, snapshot, self._kw)
        finish_span(self._span)

    def __enter__(self) -> Any:
        self._stream = self._manager.__enter__()
        return self._stream

    def __exit__(self, *exc: Any) -> Any:
        try:
            return self._manager.__exit__(*exc)
        finally:
            self._finish(exc[1])

    async def __aenter__(self) -> Any:
        self._stream = await self._manager.__aenter__()
        return self._stream

    async def __aexit__(self, *exc: Any) -> Any:
        try:
            return await self._manager.__aexit__(*exc)
        finally:
            self._finish(exc[1])


def _wrap_stream_helper(original: Any) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        manager = original(self, *args, **kwargs)
        s = safe(
            lambda: start_span(
                "anthropic.messages",
                kind="llm",
                input=_input(kwargs),
                model=kwargs.get("model"),
                model_params=params(kwargs, _EXCLUDE),
                provider="anthropic",
                streaming=True,
            )
        )
        if s is None:
            return manager
        s.start = time.time()
        return _StreamManagerProxy(manager, s, kwargs)

    wrapper.__wrapped__ = original  # type: ignore[attr-defined]
    return wrapper


def instrument() -> bool:
    try:
        from anthropic.resources.messages import messages
    except ImportError:
        return False
    spec = CallSpec(
        name="anthropic.messages",
        kind=lambda kw: "llm",
        build_input=_input,
        param_exclude=_EXCLUDE,
        on_result=_result,
        stream_handlers=_raw_stream,
        attributes=lambda client, kw: {"provider": "anthropic"},
    )
    for owner in (messages.Messages, messages.AsyncMessages):
        patch(owner, "create", wrap_method(spec))
        patch(owner, "stream", _wrap_stream_helper)
    return True
