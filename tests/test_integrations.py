"""LangChain handler, transformers pipelines and OTLP export (M10)."""

from __future__ import annotations

import sys
import types

import pytest

from tirkit.tracing import span


def test_langchain_chain_model_and_tool_spans(sink) -> None:
    pytest.importorskip("langchain_core")
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.tools import tool

    from tirkit.integrations.langchain import TirkitCallbackHandler

    @tool
    def lookup(order_id: str) -> str:
        """Look up an order."""
        return f"{order_id} shipped"

    prompt = ChatPromptTemplate.from_messages([("system", "Be brief."), ("human", "{q}")])
    chain = prompt | FakeListChatModel(responses=["It shipped."])
    handler = TirkitCallbackHandler()
    with span("app") as root:
        out = chain.invoke({"q": "Where is A1?"}, config={"callbacks": [handler]})
        lookup.invoke({"order_id": "A1"}, config={"callbacks": [handler]})
    assert out.content == "It shipped."
    names = {s.name: s for s in sink.spans}
    llm = next(s for s in sink.spans if s.kind == "llm")
    assert llm.input["messages"][0] == {"role": "system", "content": "Be brief."}
    assert llm.output["content"] == "It shipped."
    seq = next(s for s in sink.spans if s.kind == "chain" and s.parent_id == root.span_id)
    assert llm.trace_id == root.trace_id and llm.parent_id == seq.span_id
    assert names["lookup"].kind == "tool" and names["lookup"].output == "A1 shipped"
    assert not handler.runs  # every run closed


def test_transformers_pipelines_by_task(sink, monkeypatch) -> None:
    np = pytest.importorskip("numpy")
    base = types.ModuleType("transformers.pipelines.base")

    class Pipeline:
        def __init__(self, task: str) -> None:
            self.task = task
            self.model = types.SimpleNamespace(
                config=types.SimpleNamespace(_name_or_path=f"tiny/{task}")
            )

        def __call__(self, inputs=None, **kwargs):
            if self.task == "automatic-speech-recognition":
                return {"text": "hello world"}
            if self.task == "text-to-speech":
                return {"audio": np.zeros((1, 16000), dtype="float32"), "sampling_rate": 16000}
            return [{"generated_text": str(inputs) + " ok"}]

    base.Pipeline = Pipeline
    monkeypatch.setitem(sys.modules, "transformers", types.ModuleType("transformers"))
    monkeypatch.setitem(
        sys.modules, "transformers.pipelines", types.ModuleType("transformers.pipelines")
    )
    monkeypatch.setitem(sys.modules, "transformers.pipelines.base", base)
    import tirkit

    assert tirkit.instrument("transformers") == ["transformers"]
    Pipeline("text-generation")("hi", max_new_tokens=5)
    Pipeline("automatic-speech-recognition")(
        {"raw": np.zeros(32000, dtype="float32"), "sampling_rate": 16000}
    )
    Pipeline("text-to-speech")("Good morning")
    gen, asr, tts = sink.spans
    assert (
        gen.kind == "llm"
        and gen.model == "tiny/text-generation"
        and gen.model_params == {"max_new_tokens": 5}
    )
    assert asr.kind == "speech_to_text" and asr.usage["input_audio_seconds"] == 2.0
    assert asr.input["audio"].mime == "audio/wav" and asr.output == {"text": "hello world"}
    assert tts.kind == "text_to_speech" and tts.usage == {
        "output_audio_seconds": 1.0,
        "input_characters": 12,
    }
    assert tts.output["audio"].data[:4] == b"RIFF"


def test_otlp_export_round_trip() -> None:
    from tirkit.otel import otlp_to_spans, spans_to_otlp
    from tirkit.tracing import Span

    s = Span(
        name="chat",
        kind="llm",
        model="gpt-4o",
        input={"q": "hi"},
        output="yo",
        usage={"input_tokens": 5, "output_tokens": 2},
        session_id="c1",
        user_id="u1",
    )
    s.end = s.start + 1
    back = otlp_to_spans(spans_to_otlp([s]))[0]
    assert (back.trace_id, back.span_id, back.kind, back.model) == (
        s.trace_id,
        s.span_id,
        "llm",
        "gpt-4o",
    )
    assert back.usage == {"input_tokens": 5, "output_tokens": 2}
    assert back.session_id == "c1" and back.user_id == "u1"
    assert back.input == {"q": "hi"}
