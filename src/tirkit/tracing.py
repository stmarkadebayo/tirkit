"""Tracing: record every model call, tool call and step as a span (SPEC FR-1, FR-2).

Two ways in::

    with tirkit.span("answer", kind="llm", model="gpt-4o", input=messages) as s:
        reply = call_model(messages)
        s.output = reply
        s.set_usage(input_tokens=812, output_tokens=95)

    @tirkit.trace(kind="tool")
    def search(query: str) -> list[str]: ...

Spans nest automatically through context variables (threads and asyncio tasks each get their own
context; use :func:`copy_context_run` to carry a parent into a thread pool). Finished spans go to a
sink: one passed explicitly, or the default set by :func:`tirkit.init`. Tracing never raises into
user code.
"""

from __future__ import annotations

import contextvars
import fnmatch
import functools
import inspect
import logging
import time
import zlib
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, overload

from tirkit import pricing
from tirkit.serialize import to_jsonable
from tirkit.types import Span

log = logging.getLogger("tirkit")

KINDS = frozenset(
    {
        "generic",
        "llm",  # text or chat generation, including vision-language input
        "embedding",
        "rerank",
        "retrieval",
        "tool",
        "agent",
        "chain",
        "image_generation",
        "speech_to_text",
        "text_to_speech",
        "speech_to_speech",
        "guardrail",
        "evaluator",
        "vector_db",  # a vector store query or upsert (Chroma, Pinecone, pgvector...)
        "mcp_tool",  # a tool called through the Model Context Protocol
        "human",  # human in the loop: waiting for an approval or an answer
        "computer_use",  # a computer-use action (click, type, screenshot)
        "retry",  # a call retried until it worked; children are the attempts
        "fallback",  # alternatives tried in order; children are the options tried
    }
)


class SpanSink(Protocol):
    def save_span(self, span: Span) -> None: ...


class _NoopSink:
    def save_span(self, span: Span) -> None:
        return None


_current: contextvars.ContextVar[Span | None] = contextvars.ContextVar("tirkit_span", default=None)
_trace_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "tirkit_trace_ctx", default=None
)
# Set by ``Prompt.use()``: spans record the prompt version that produced them.
current_prompt: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tirkit_prompt", default=None
)
_default_sink: SpanSink = _NoopSink()


_ctx_sink: contextvars.ContextVar[SpanSink | None] = contextvars.ContextVar(
    "tirkit_sink", default=None
)


@contextmanager
def sink_context(sink: SpanSink) -> Iterator[None]:
    """Send spans finished inside the block to ``sink`` (e.g. one project's store on a server
    that works for many projects)."""
    token = _ctx_sink.set(sink)
    try:
        yield
    finally:
        _ctx_sink.reset(token)


def set_default_sink(sink: SpanSink | None) -> None:
    """Where spans go when no sink is passed. ``None`` turns tracing output off."""
    global _default_sink
    _default_sink = sink if sink is not None else _NoopSink()


def get_default_sink() -> SpanSink:
    return _default_sink


# -- sampling -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SamplingRule:
    """Sample rate for traces whose root span matches. ``name`` is a glob (``health*``);
    a rule with neither ``name`` nor ``kind`` matches every trace."""

    rate: float
    name: str | None = None
    kind: str | None = None

    def matches(self, name: str, kind: str) -> bool:
        if self.kind is not None and self.kind != kind:
            return False
        return self.name is None or fnmatch.fnmatchcase(name, self.name)


def _check_rate(rate: float) -> float:
    rate = float(rate)
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"sample rate must be between 0 and 1, got {rate}")
    return rate


class Sampler:
    """Head sampling per trace: decided once at the root span, inherited by every child, so a
    trace is kept whole or dropped whole. The decision hashes the trace id, so it is the same
    in every process that sees the trace. With ``keep_errors`` a span that fails is exported
    anyway, together with every span of its trace that finishes after it (its ancestors)."""

    def __init__(
        self,
        rate: float = 1.0,
        rules: list[SamplingRule | dict[str, Any]] | None = None,
        keep_errors: bool = True,
    ) -> None:
        self.rate = _check_rate(rate)
        self.rules = [r if isinstance(r, SamplingRule) else SamplingRule(**r) for r in rules or []]
        for r in self.rules:
            _check_rate(r.rate)
        self.keep_errors = keep_errors

    def rate_for(self, name: str, kind: str) -> float:
        for rule in self.rules:
            if rule.matches(name, kind):
                return rule.rate
        return self.rate

    def keep_trace(self, trace_id: str, name: str, kind: str) -> bool:
        rate = self.rate_for(name, kind)
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        return zlib.crc32(trace_id.encode()) / 0xFFFFFFFF < rate


_sampler: Sampler | None = None
_SAMPLE = "_tirkit_sample"  # instance key on a Span holding its trace's sampling decision


def set_sampling(
    rate: float | None = 1.0,
    rules: list[SamplingRule | dict[str, Any]] | None = None,
    keep_errors: bool = True,
) -> None:
    """Keep a fraction of traces. ``set_sampling(None)`` (or rate 1 with no rules) turns
    sampling off. Rules are checked in order against the root span; the first match wins."""
    global _sampler
    if rate is None or (float(rate) >= 1.0 and not rules):
        _check_rate(1.0 if rate is None else rate)
        _sampler = None
        return
    _sampler = Sampler(rate, rules, keep_errors)


def get_sampler() -> Sampler | None:
    return _sampler


def _safe_reset(token: contextvars.Token[Span | None]) -> None:
    try:
        _current.reset(token)
    except ValueError:  # generator resumed in another context: just clear it
        _current.set(None)


def current_span() -> Span | None:
    return _current.get()


@contextmanager
def trace_context(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    release: str | None = None,
    tags: list[str] | None = None,
) -> Iterator[None]:
    """Set trace-level fields for every span started inside the block."""
    base = dict(_trace_ctx.get() or {})
    for k, v in (("session_id", session_id), ("user_id", user_id), ("release", release)):
        if v is not None:
            base[k] = v
    if tags:
        base["tags"] = list(dict.fromkeys([*base.get("tags", []), *tags]))
    token = _trace_ctx.set(base)
    try:
        yield
    finally:
        _trace_ctx.reset(token)


def start_span(
    name: str,
    *,
    parent: Span | None = None,
    kind: str = "generic",
    input: Any = None,
    model: str | None = None,
    model_params: dict[str, Any] | None = None,
    **attributes: Any,
) -> Span:
    """Create a span without making it current. Pair with :func:`finish_span` (for streams)."""
    parent = parent if parent is not None else _current.get()
    s = Span(name=name, kind=kind, input=to_jsonable(input) if input is not None else None)
    s.attributes = dict(attributes)
    s.model = model if model is not None else attributes.get("model")
    s.prompt_version = current_prompt.get()
    if model_params:
        s.model_params = to_jsonable(model_params)
    ctx = _trace_ctx.get() or {}
    if parent is not None:
        s.trace_id, s.parent_id = parent.trace_id, parent.span_id
        s.session_id, s.user_id, s.release = parent.session_id, parent.user_id, parent.release
        s.tags = list(parent.tags)
        decision = parent.__dict__.get(_SAMPLE)
        if decision is not None:
            s.__dict__[_SAMPLE] = decision
    elif _sampler is not None:
        # [keep, keep_errors], shared by every span of the trace.
        s.__dict__[_SAMPLE] = [_sampler.keep_trace(s.trace_id, name, kind), _sampler.keep_errors]
    for k in ("session_id", "user_id", "release"):
        if ctx.get(k) is not None:
            setattr(s, k, ctx[k])
    if ctx.get("tags"):
        s.tags = list(dict.fromkeys([*s.tags, *ctx["tags"]]))
    return s


def finish_span(s: Span, sink: SpanSink | None = None) -> None:
    """Close a span, compute cost if possible, and hand it to the sink. Never raises."""
    try:
        if s.end is None:
            s.end = time.time()
        decision = s.__dict__.get(_SAMPLE)
        if decision is not None and not decision[0]:
            if not (decision[1] and s.status == "error"):
                return
            decision[0] = True  # keep the rest of this trace: the error's ancestors
        if s.output is not None:
            s.output = to_jsonable(s.output)
        if s.cost_usd is None and s.usage:
            s.cost_usd = pricing.cost(s.model, s.usage)
        target = sink if sink is not None else (_ctx_sink.get() or _default_sink)
        target.save_span(s)
    except Exception:  # noqa: BLE001 - tracing must never break the caller
        log.warning("tirkit: failed to export span %s", s.name, exc_info=True)


@contextmanager
def span(
    name: str,
    sink: SpanSink | None = None,
    parent: Span | None = None,
    *,
    kind: str = "generic",
    input: Any = None,
    **attributes: Any,
) -> Iterator[Span]:
    """Time a block of work and hand the finished span to a sink.

    Keyword arguments become attributes; ``model=`` also fills the model column.
    """
    s = start_span(name, parent=parent, kind=kind, input=input, **attributes)
    if "model" in attributes:  # keep it as an attribute too, as passed
        s.attributes["model"] = attributes["model"]
    token = _current.set(s)
    try:
        yield s
    except BaseException as exc:
        s.record_error(exc)
        raise
    finally:
        _safe_reset(token)
        finish_span(s, sink)


F = TypeVar("F", bound=Callable[..., Any])


def _capture_args(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        data = dict(bound.arguments)
        data.pop("self", None)
        data.pop("cls", None)
        return data
    except (TypeError, ValueError):
        return {"args": list(args), "kwargs": kwargs}


@overload
def trace(fn: F) -> F: ...


@overload
def trace(
    fn: None = None,
    *,
    name: str | None = None,
    kind: str = "generic",
    sink: SpanSink | None = None,
    capture_input: bool = True,
    capture_output: bool = True,
    **attributes: Any,
) -> Callable[[F], F]: ...


def trace(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    kind: str = "generic",
    sink: SpanSink | None = None,
    capture_input: bool = True,
    capture_output: bool = True,
    **attributes: Any,
) -> Any:
    """Decorator: trace every call of a function (sync, async, generator or async generator)."""

    def decorate(f: Callable[..., Any]) -> Callable[..., Any]:
        span_name = name or f.__qualname__

        def begin(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Span:
            inp = _capture_args(f, args, kwargs) if capture_input else None
            return start_span(span_name, kind=kind, input=inp, **attributes)

        if inspect.isasyncgenfunction(f):

            @functools.wraps(f)
            async def agen_wrapper(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
                s = begin(args, kwargs)
                token = _current.set(s)
                items: list[Any] = []
                try:
                    async for item in f(*args, **kwargs):
                        if capture_output:
                            items.append(item)
                        yield item
                except BaseException as exc:
                    s.record_error(exc)
                    raise
                finally:
                    _safe_reset(token)
                    if capture_output:
                        s.output = items
                    finish_span(s, sink)

            return agen_wrapper

        if inspect.iscoroutinefunction(f):

            @functools.wraps(f)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                s = begin(args, kwargs)
                token = _current.set(s)
                try:
                    result = await f(*args, **kwargs)
                    if capture_output:
                        s.output = result
                    return result
                except BaseException as exc:
                    s.record_error(exc)
                    raise
                finally:
                    _safe_reset(token)
                    finish_span(s, sink)

            return async_wrapper

        if inspect.isgeneratorfunction(f):

            @functools.wraps(f)
            def gen_wrapper(*args: Any, **kwargs: Any) -> Iterator[Any]:
                s = begin(args, kwargs)
                token = _current.set(s)
                items: list[Any] = []
                try:
                    for item in f(*args, **kwargs):
                        if capture_output:
                            items.append(item)
                        yield item
                except BaseException as exc:
                    s.record_error(exc)
                    raise
                finally:
                    _safe_reset(token)
                    if capture_output:
                        s.output = items
                    finish_span(s, sink)

            return gen_wrapper

        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            s = begin(args, kwargs)
            token = _current.set(s)
            try:
                result = f(*args, **kwargs)
                if capture_output:
                    s.output = result
                return result
            except BaseException as exc:
                s.record_error(exc)
                raise
            finally:
                _safe_reset(token)
                finish_span(s, sink)

        return wrapper

    if fn is not None:
        return decorate(fn)
    return decorate


def copy_context_run(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``fn`` so it runs in a copy of the current context (keeps the parent span in threads).

    Example: ``pool.submit(copy_context_run(work), item)``.
    """
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        return ctx.copy().run(fn, *args, **kwargs)

    return run
