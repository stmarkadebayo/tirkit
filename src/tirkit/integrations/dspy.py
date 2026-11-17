"""DSPy: a callback that records modules (``Predict``, ``ChainOfThought``, ``ReAct``...), language
model calls and tools as Tirkit spans, nested the way DSPy runs them.

    import dspy
    from tirkit.integrations.dspy import TirkitCallback
    dspy.configure(lm=lm, callbacks=[TirkitCallback()])

or ``tirkit.instrument("dspy")``, which appends it to ``dspy.settings.callbacks``.
"""

from __future__ import annotations

import contextvars
import threading
from typing import Any

from tirkit.serialize import to_jsonable
from tirkit.tracing import Span, current_span, finish_span, start_span

_current: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "tirkit_dspy_span", default=None
)
_BASE: type | None = None


def _callback_base() -> type:
    from dspy.utils.callback import BaseCallback

    return BaseCallback


class _Mixin:
    """Callback logic; mixed into DSPy's ``BaseCallback`` on first use."""

    def __init__(self) -> None:
        self.runs: dict[str, tuple[Span, contextvars.Token[Span | None], Any]] = {}
        self._lock = threading.Lock()

    def _start(self, call_id: str, name: str, kind: str, inp: Any, instance: Any = None) -> None:
        parent = _current.get() or current_span()
        s = start_span(name, parent=parent, kind=kind, input=to_jsonable(inp))
        token = _current.set(s)
        with self._lock:
            self.runs[call_id] = (s, token, instance)

    def _end(self, call_id: str, outputs: Any, exception: BaseException | None) -> Span | None:
        with self._lock:
            entry = self.runs.pop(call_id, None)
        if entry is None:
            return None
        s, token, _ = entry
        try:
            _current.reset(token)
        except ValueError:  # ended in another context
            _current.set(None)
        if exception is not None:
            s.record_error(exception)
        elif outputs is not None:
            s.output = to_jsonable(outputs)
        return s

    # modules ---------------------------------------------------------------------------------

    def on_module_start(self, call_id: str, instance: Any, inputs: dict[str, Any]) -> None:
        name = type(instance).__name__
        kind = "agent" if name.lower() in {"react", "codeact", "programofthought"} else "chain"
        inp = inputs
        if set(inputs) <= {"args", "kwargs"}:  # flatten ``forward(question=...)`` for readability
            inp = {**(inputs.get("kwargs") or {})}
            if inputs.get("args"):
                inp["args"] = inputs["args"]
        self._start(call_id, name, kind, inp, instance)

    def on_module_end(
        self, call_id: str, outputs: Any, exception: BaseException | None = None
    ) -> None:
        s = self._end(call_id, outputs, exception)
        if s is not None:
            finish_span(s)

    # language models -------------------------------------------------------------------------

    def on_lm_start(self, call_id: str, instance: Any, inputs: dict[str, Any]) -> None:
        inp = {k: v for k, v in inputs.items() if k in ("messages", "prompt")}
        prompt = inp.get("prompt")
        nested = (
            prompt.get("messages")
            if isinstance(prompt, dict)
            else getattr(prompt, "messages", None)
        )
        if nested is not None:  # newer DSPy passes a request object holding the messages
            inp = {"messages": to_jsonable(nested)}
        self._start(call_id, "lm", "llm", inp, instance)
        s = self.runs[call_id][0]
        s.model = getattr(instance, "model", None)
        kwargs = {**(getattr(instance, "kwargs", None) or {})}
        kwargs.update({k: v for k, v in inputs.items() if k not in ("messages", "prompt")})
        s.model_params = to_jsonable(
            {
                k: v
                for k, v in kwargs.items()
                if k in ("temperature", "max_tokens", "top_p", "n", "seed")
            }
        )

    def on_lm_end(self, call_id: str, outputs: Any, exception: BaseException | None = None) -> None:
        with self._lock:
            instance = self.runs.get(call_id, (None, None, None))[2]
        s = self._end(call_id, outputs, exception)
        if s is None:
            return
        try:
            last = (getattr(instance, "history", None) or [None])[-1] or {}
            usage = last.get("usage") or {}
            s.set_usage(
                input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
                output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
            )
            s.model = last.get("response_model") or s.model
            if last.get("cost") is not None and s.cost_usd is None:
                s.attributes["dspy_cost_usd"] = last["cost"]
        except Exception:  # noqa: BLE001 - tracing must never break the program
            pass
        finish_span(s)

    # tools -----------------------------------------------------------------------------------

    def on_tool_start(self, call_id: str, instance: Any, inputs: dict[str, Any]) -> None:
        self._start(call_id, getattr(instance, "name", None) or "tool", "tool", inputs, instance)

    def on_tool_end(
        self, call_id: str, outputs: Any, exception: BaseException | None = None
    ) -> None:
        s = self._end(call_id, outputs, exception)
        if s is not None:
            finish_span(s)

    # evaluation (dspy.Evaluate) --------------------------------------------------------------

    def on_evaluate_start(self, call_id: str, instance: Any, inputs: dict[str, Any]) -> None:
        self._start(
            call_id,
            "evaluate",
            "evaluator",
            {k: v for k, v in inputs.items() if k != "program"},
            instance,
        )

    def on_evaluate_end(
        self, call_id: str, outputs: Any, exception: BaseException | None = None
    ) -> None:
        s = self._end(call_id, outputs, exception)
        if s is not None:
            finish_span(s)


def TirkitCallback() -> Any:  # noqa: N802 - a class from the caller's point of view
    """Create the callback (DSPy is imported here, not at module import)."""
    global _BASE
    if _BASE is None:
        base = _callback_base()
        _BASE = type("TirkitCallback", (_Mixin, base), {})
    return _BASE()


def instrument() -> bool:
    try:
        import dspy
    except ImportError:
        return False
    current = list(dspy.settings.get("callbacks") or [])
    if any(type(c).__name__ == "TirkitCallback" for c in current):
        return True
    dspy.configure(callbacks=[*current, TirkitCallback()])
    return True
