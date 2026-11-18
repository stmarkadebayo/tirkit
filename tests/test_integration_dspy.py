"""DSPy callback with DSPy's own DummyLM (no network)."""

from __future__ import annotations

import pytest

dspy = pytest.importorskip("dspy")

import tirkit  # noqa: E402
from tirkit.integrations.dspy import TirkitCallback  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_dspy():
    yield
    dspy.configure(callbacks=[])


def test_module_and_lm_spans_nest(sink) -> None:
    from dspy.utils import DummyLM

    lm = DummyLM([{"answer": "Paris"}])
    dspy.configure(lm=lm, callbacks=[TirkitCallback()])
    with tirkit.span("request") as root:
        out = dspy.Predict("question -> answer")(question="Capital of France?")
    assert out.answer == "Paris"
    predict = next(s for s in sink.spans if s.name == "Predict")
    llm = next(s for s in sink.spans if s.kind == "llm")
    assert predict.kind == "chain" and predict.parent_id == root.span_id
    assert predict.input["question"] == "Capital of France?"
    assert llm.parent_id == predict.span_id and "Paris" in str(llm.output)
    assert llm.input["messages"], "the prompt sent to the model is recorded"


def test_tool_and_error(sink) -> None:
    from dspy.utils import DummyLM

    def lookup(order: str) -> str:
        """Look up an order."""
        return f"{order} shipped"

    tool = dspy.Tool(lookup)
    dspy.configure(lm=DummyLM([{}]), callbacks=[TirkitCallback()])
    assert tool(order="A1") == "A1 shipped"
    t = next(s for s in sink.spans if s.kind == "tool")
    assert t.name == "lookup" and t.output == "A1 shipped"

    def boom() -> str:
        raise ValueError("bad")

    with pytest.raises(Exception):  # noqa: B017 - DSPy wraps tool errors
        dspy.Tool(boom)()
    assert any(s.kind == "tool" and s.status == "error" for s in sink.spans)


def test_instrument_adds_the_callback_once() -> None:
    assert tirkit.instrument("dspy") == ["dspy"]
    assert tirkit.instrument("dspy") == ["dspy"]
    names = [type(c).__name__ for c in dspy.settings.callbacks]
    assert names.count("TirkitCallback") == 1
