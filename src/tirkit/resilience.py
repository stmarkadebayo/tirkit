"""Retry and fallback, traced: one parent span per call, one child span per attempt or option.

::

    @tirkit.retry(attempts=3, retry_on=(RateLimitError,), backoff=0.5)
    def answer(q): ...

    answer = tirkit.fallback(ask_gpt, ask_claude, name="answer")

A retry span (kind ``retry``) records ``retry.attempts`` and ``retry.max_attempts``; each attempt
(kind ``generic``) records ``retry.attempt`` and its error. A fallback span (kind ``fallback``)
records ``fallback.chosen`` and ``fallback.chosen_name``; each option tried records
``fallback.index``. Model calls made inside an attempt nest under it, so the trace shows which
attempt failed and why.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from collections.abc import Callable
from typing import Any, TypeVar

from tirkit.tracing import span

F = TypeVar("F", bound=Callable[..., Any])


def _delay(backoff: float, max_backoff: float, attempt: int) -> float:
    return min(backoff * (2 ** (attempt - 1)), max_backoff) if backoff > 0 else 0.0


def _name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__name__", None) or type(fn).__name__


def with_retry(
    fn: Callable[..., Any],
    *args: Any,
    attempts: int = 3,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    backoff: float = 0.0,
    max_backoff: float = 30.0,
    name: str | None = None,
    **kwargs: Any,
) -> Any:
    """Call ``fn(*args, **kwargs)`` up to ``attempts`` times, retrying on ``retry_on``.

    Returns the result, or a coroutine for an async ``fn``. Waits ``backoff * 2**(n-1)``
    seconds (capped at ``max_backoff``) between attempts.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    label = name or f"retry:{_name(fn)}"
    if inspect.iscoroutinefunction(fn):
        return _retry_async(fn, args, kwargs, attempts, retry_on, backoff, max_backoff, label)
    with span(label, kind="retry") as parent:
        parent.attributes["retry.max_attempts"] = attempts
        for n in range(1, attempts + 1):
            parent.attributes["retry.attempts"] = n
            try:
                with span(f"attempt {n}") as att:
                    att.attributes["retry.attempt"] = n
                    result = fn(*args, **kwargs)
            except retry_on:
                if n == attempts:
                    raise
                time.sleep(_delay(backoff, max_backoff, n))
                continue
            parent.output = result
            return result
    raise AssertionError("unreachable")  # pragma: no cover


async def _retry_async(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    attempts: int,
    retry_on: tuple[type[BaseException], ...],
    backoff: float,
    max_backoff: float,
    label: str,
) -> Any:
    with span(label, kind="retry") as parent:
        parent.attributes["retry.max_attempts"] = attempts
        for n in range(1, attempts + 1):
            parent.attributes["retry.attempts"] = n
            try:
                with span(f"attempt {n}") as att:
                    att.attributes["retry.attempt"] = n
                    result = await fn(*args, **kwargs)
            except retry_on:
                if n == attempts:
                    raise
                await asyncio.sleep(_delay(backoff, max_backoff, n))
                continue
            parent.output = result
            return result
    raise AssertionError("unreachable")  # pragma: no cover


def retry(
    attempts: int = 3,
    *,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    backoff: float = 0.0,
    max_backoff: float = 30.0,
    name: str | None = None,
) -> Callable[[F], F]:
    """Decorator form of :func:`with_retry` (sync or async functions)."""

    def decorate(fn: F) -> F:
        opts: dict[str, Any] = {
            "attempts": attempts,
            "retry_on": retry_on,
            "backoff": backoff,
            "max_backoff": max_backoff,
            "name": name or f"retry:{_name(fn)}",
        }

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await with_retry(fn, *args, **opts, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return with_retry(fn, *args, **opts, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


def fallback(
    *options: Callable[..., Any],
    name: str = "fallback",
    catch: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[..., Any]:
    """A callable that tries ``options`` in order with the same arguments and returns the first
    result that does not raise one of ``catch``. If all fail, the last error is raised."""
    if not options:
        raise ValueError("fallback needs at least one option")
    if all(inspect.iscoroutinefunction(o) for o in options):

        async def run_async(*args: Any, **kwargs: Any) -> Any:
            with span(name, kind="fallback") as parent:
                parent.attributes["fallback.options"] = len(options)
                for i, opt in enumerate(options):
                    try:
                        with span(_name(opt)) as att:
                            att.attributes["fallback.index"] = i
                            result = await opt(*args, **kwargs)
                    except catch:
                        if i == len(options) - 1:
                            raise
                        continue
                    parent.set_attributes(
                        **{"fallback.chosen": i, "fallback.chosen_name": _name(opt)}
                    )
                    parent.output = result
                    return result

        return run_async

    def run(*args: Any, **kwargs: Any) -> Any:
        with span(name, kind="fallback") as parent:
            parent.attributes["fallback.options"] = len(options)
            for i, opt in enumerate(options):
                try:
                    with span(_name(opt)) as att:
                        att.attributes["fallback.index"] = i
                        result = opt(*args, **kwargs)
                except catch:
                    if i == len(options) - 1:
                        raise
                    continue
                parent.set_attributes(**{"fallback.chosen": i, "fallback.chosen_name": _name(opt)})
                parent.output = result
                return result

    return run
