"""Tirkit: LLM evals and observability."""

from tirkit.client import Tirkit, feedback, flush, get_client, init
from tirkit.media import Attachment
from tirkit.pricing import register_price
from tirkit.prompts import Prompt, get_prompt
from tirkit.resilience import fallback, retry, with_retry
from tirkit.tracing import (
    Span,
    copy_context_run,
    current_span,
    finish_span,
    span,
    start_span,
    trace,
    trace_context,
)

__version__ = "0.1.0"

__all__ = [
    "Attachment",
    "Tirkit",
    "Span",
    "copy_context_run",
    "current_span",
    "fallback",
    "feedback",
    "finish_span",
    "flush",
    "get_prompt",
    "Prompt",
    "get_client",
    "init",
    "instrument",
    "register_price",
    "retry",
    "span",
    "start_span",
    "trace",
    "trace_context",
    "with_retry",
]


def instrument(*providers: str) -> list[str]:
    """Auto-trace installed model SDKs (``"openai"``, ``"anthropic"``; default: all found).

    Returns the providers that were instrumented.
    """
    from tirkit.instrumentation import instrument as _instrument

    return _instrument(*providers)
