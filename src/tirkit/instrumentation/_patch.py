"""Patching helpers shared by provider instrumentations.

A provider wrapper starts a span before the SDK call, fills it from the result (or wraps a stream
so the span closes when the stream ends), and never lets a tracing error reach the caller.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from tirkit.media import Attachment
from tirkit.tracing import Span, finish_span, start_span

log = logging.getLogger("tirkit")

MAX_MEDIA_BYTES = 50 * 1024 * 1024

_patched: list[tuple[Any, str, Any]] = []


def patch(owner: Any, attr: str, make: Callable[[Any], Any]) -> bool:
    """Replace ``owner.attr`` with ``make(original)`` once. Returns False if missing."""
    original = owner.__dict__.get(attr) if hasattr(owner, "__dict__") else None
    if original is None:
        return False
    if getattr(original, "__tirkit_wrapped__", False):
        return True
    wrapped = make(original)
    wrapped.__tirkit_wrapped__ = True
    setattr(owner, attr, wrapped)
    _patched.append((owner, attr, original))
    return True


def unpatch_all() -> None:
    while _patched:
        owner, attr, original = _patched.pop()
        setattr(owner, attr, original)


def safe(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - tracing must never break the caller
        log.debug("tirkit: instrumentation hook failed", exc_info=True)
        return None


def get(obj: Any, *path: str) -> Any:
    """Read nested attributes or dict keys, returning None when any step is missing."""
    cur = obj
    for key in path:
        if cur is None:
            return None
        cur = cur.get(key) if isinstance(cur, dict) else getattr(cur, key, None)
    return cur


def params(kwargs: dict[str, Any], exclude: set[str]) -> dict[str, Any]:
    skip = exclude | {"extra_headers", "extra_query", "extra_body", "timeout", "stream"}
    return {
        k: v
        for k, v in kwargs.items()
        if k not in skip and type(v).__name__ not in {"NotGiven", "Omit"}
    }


def file_to_media(f: Any) -> Any:
    """Turn an SDK file argument (bytes, path, file object, (name, data, mime) tuple) into an
    :class:`Attachment` without disturbing the caller's file position."""
    try:
        name, mime = None, None
        if isinstance(f, tuple):
            name = f[0] if len(f) > 0 else None
            mime = f[2] if len(f) > 2 else None
            f = f[1] if len(f) > 1 else None
        data: bytes | None = None
        if isinstance(f, bytes | bytearray):
            data = bytes(f)
        elif isinstance(f, str) or hasattr(f, "__fspath__"):
            from pathlib import Path

            p = Path(f)
            name = name or p.name
            if p.stat().st_size <= MAX_MEDIA_BYTES:
                data = p.read_bytes()
        elif hasattr(f, "read") and hasattr(f, "seek") and hasattr(f, "tell"):
            pos = f.tell()
            data = f.read(MAX_MEDIA_BYTES + 1)
            f.seek(pos)
            name = name or getattr(f, "name", None)
            if isinstance(data, str):
                data = data.encode()
            if len(data) > MAX_MEDIA_BYTES:
                data = None
        if data is None:
            return {"file": str(name) if name else "<file>"}
        return Attachment(data=data, mime=mime)
    except Exception:  # noqa: BLE001
        return {"file": "<unreadable>"}


def wav_seconds(data: bytes | None) -> float | None:
    """Duration of WAV audio, from its header (None for other formats)."""
    if not data or data[:4] != b"RIFF":
        return None
    try:
        with wave.open(BytesIO(data)) as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else None
    except Exception:  # noqa: BLE001
        return None


@dataclass
class CallSpec:
    """How to trace one SDK method."""

    name: str
    kind: Callable[[dict[str, Any]], str]
    build_input: Callable[[dict[str, Any]], Any]
    param_exclude: set[str] = field(default_factory=set)
    on_result: Callable[[Span, Any, dict[str, Any]], None] | None = None
    is_stream: Callable[[dict[str, Any], Any], bool] = lambda kw, res: bool(kw.get("stream"))
    # Factory returning (on_item, on_done) callbacks for a stream.
    stream_handlers: (
        Callable[[Span, dict[str, Any]], tuple[Callable[[Any], None], Callable[[], None]]] | None
    ) = None
    attributes: Callable[[Any, dict[str, Any]], dict[str, Any]] | None = None
    # Model parameters; by default every keyword argument not excluded above.
    build_params: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _begin(spec: CallSpec, client: Any, kwargs: dict[str, Any]) -> Span:
    attrs = safe(spec.attributes, client, kwargs) if spec.attributes else None
    return start_span(
        spec.name,
        kind=safe(spec.kind, kwargs) or "llm",
        input=safe(spec.build_input, kwargs),
        model=kwargs.get("model") if isinstance(kwargs.get("model"), str) else None,
        model_params=(
            safe(spec.build_params, kwargs)
            if spec.build_params
            else params(kwargs, spec.param_exclude)
        ),
        **(attrs or {}),
    )


def _complete(spec: CallSpec, s: Span, result: Any, kwargs: dict[str, Any]) -> None:
    if spec.on_result is not None:
        safe(spec.on_result, s, result, kwargs)
    finish_span(s)


def wrap_method(spec: CallSpec) -> Callable[[Any], Any]:
    """Factory for :func:`patch`: wraps a sync or async SDK method."""

    def make(original: Any) -> Any:
        if inspect.iscoroutinefunction(original):

            @functools.wraps(original)
            async def async_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                s = safe(_begin, spec, self, kwargs)
                if s is None:
                    return await original(self, *args, **kwargs)
                try:
                    result = await original(self, *args, **kwargs)
                except BaseException as exc:
                    s.record_error(exc)
                    finish_span(s)
                    raise
                if spec.stream_handlers and safe(spec.is_stream, kwargs, result):
                    handlers = safe(spec.stream_handlers, s, kwargs)
                    if handlers:
                        return AsyncStreamProxy(result, s, *handlers)
                _complete(spec, s, result, kwargs)
                return result

            return async_wrapper

        async def finish_awaitable(pending: Any, s: Span, kwargs: dict[str, Any]) -> Any:
            # SDK async methods are often sync decorators returning a coroutine.
            try:
                result = await pending
            except BaseException as exc:
                s.record_error(exc)
                finish_span(s)
                raise
            if spec.stream_handlers and safe(spec.is_stream, kwargs, result):
                handlers = safe(spec.stream_handlers, s, kwargs)
                if handlers:
                    return AsyncStreamProxy(result, s, *handlers)
            _complete(spec, s, result, kwargs)
            return result

        @functools.wraps(original)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            s = safe(_begin, spec, self, kwargs)
            if s is None:
                return original(self, *args, **kwargs)
            try:
                result = original(self, *args, **kwargs)
            except BaseException as exc:
                s.record_error(exc)
                finish_span(s)
                raise
            if inspect.isawaitable(result):
                return finish_awaitable(result, s, kwargs)
            if spec.stream_handlers and safe(spec.is_stream, kwargs, result):
                handlers = safe(spec.stream_handlers, s, kwargs)
                if handlers:
                    return SyncStreamProxy(result, s, *handlers)
            _complete(spec, s, result, kwargs)
            return result

        return wrapper

    return make


class _StreamBase:
    def __init__(
        self,
        stream: Any,
        span: Span,
        on_item: Callable[[Any], None],
        on_done: Callable[[], None],
    ) -> None:
        self._stream = stream
        self._span = span
        self._on_item = on_item
        self._on_done = on_done
        self._first = True
        self._finished = False

    def _item(self, item: Any) -> None:
        if self._first:
            self._first = False
            self._span.ttft_ms = (time.time() - self._span.start) * 1000
        safe(self._on_item, item)

    def _finish(self, exc: BaseException | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        if exc is not None and not isinstance(exc, StopIteration | StopAsyncIteration):
            self._span.record_error(exc)
        safe(self._on_done)
        finish_span(self._span)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def __del__(self) -> None:  # stream abandoned before the end
        try:
            self._finish()
        except Exception:  # noqa: BLE001
            pass


class SyncStreamProxy(_StreamBase):
    """Wraps an SDK stream: passes every item through and closes the span at the end."""

    def __iter__(self) -> Any:
        try:
            for item in self._stream:
                self._item(item)
                yield item
        except BaseException as exc:
            self._finish(exc)
            raise
        self._finish()

    def __next__(self) -> Any:
        try:
            item = next(self._stream)
        except StopIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._finish(exc)
            raise
        self._item(item)
        return item

    def __enter__(self) -> SyncStreamProxy:
        enter = getattr(self._stream, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *exc: Any) -> Any:
        try:
            exit_ = getattr(self._stream, "__exit__", None)
            return exit_(*exc) if exit_ is not None else None
        finally:
            self._finish(exc[1])

    def close(self) -> None:
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                close()
        finally:
            self._finish()


class AsyncStreamProxy(_StreamBase):
    """Async counterpart of :class:`SyncStreamProxy`."""

    def __aiter__(self) -> Any:
        return self._agen()

    async def _agen(self) -> Any:
        try:
            async for item in self._stream:
                self._item(item)
                yield item
        except BaseException as exc:
            self._finish(exc)
            raise
        self._finish()

    async def __anext__(self) -> Any:
        try:
            item = await self._stream.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._finish(exc)
            raise
        self._item(item)
        return item

    async def __aenter__(self) -> AsyncStreamProxy:
        enter = getattr(self._stream, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        try:
            exit_ = getattr(self._stream, "__aexit__", None)
            return await exit_(*exc) if exit_ is not None else None
        finally:
            self._finish(exc[1])

    async def close(self) -> None:
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                res = close()
                if inspect.isawaitable(res):
                    await res
        finally:
            self._finish()
