"""LlamaIndex handlers with a real index, a fake LLM and a fake embedding model (no network)."""

from __future__ import annotations

import pytest

pytest.importorskip("llama_index.core")

import tirkit  # noqa: E402
from tirkit.integrations import llamaindex as mod  # noqa: E402


@pytest.fixture(autouse=True)
def _instrumented():
    assert tirkit.instrument("llamaindex") == ["llamaindex"]
    yield
    mod._spans.clear()


def _llm():
    from llama_index.core.base.llms.types import CompletionResponse, LLMMetadata
    from llama_index.core.llms import CustomLLM
    from llama_index.core.llms.callbacks import llm_completion_callback

    class FakeLLM(CustomLLM):
        @property
        def metadata(self) -> LLMMetadata:
            return LLMMetadata(model_name="gpt-4o-mini")

        @llm_completion_callback()
        def complete(self, prompt: str, formatted: bool = False, **kw):
            return CompletionResponse(
                text="Returns take 30 days.",
                raw={"usage": {"prompt_tokens": 40, "completion_tokens": 6}},
            )

        @llm_completion_callback()
        def stream_complete(self, prompt: str, formatted: bool = False, **kw):
            def gen():
                yield CompletionResponse(text="Returns", delta="Returns")
                yield CompletionResponse(text="Returns ok", delta=" ok")

            return gen()

    return FakeLLM()


def _index():
    from llama_index.core import Document, Settings, VectorStoreIndex
    from llama_index.core.embeddings import MockEmbedding

    Settings.embed_model = MockEmbedding(embed_dim=8)
    Settings.llm = _llm()
    return VectorStoreIndex.from_documents([Document(text="Returns are accepted for 30 days.")])


def test_query_engine_traces_retrieval_llm_and_embeddings(sink) -> None:
    index = _index()
    sink.spans.clear()
    with tirkit.span("request") as root:
        answer = index.as_query_engine().query("What is the returns policy?")
    assert "30 days" in str(answer)
    spans = {s.span_id: s for s in sink.spans}
    llm = next(s for s in sink.spans if s.kind == "llm")
    retrieval = next(s for s in sink.spans if s.kind == "retrieval")
    assert llm.model == "gpt-4o-mini"
    assert llm.usage == {"input_tokens": 40, "output_tokens": 6} and llm.cost_usd > 0
    assert "returns policy" in str(llm.input).lower() or "30 days" in str(llm.input)
    assert llm.output == "Returns take 30 days." or "30 days" in str(llm.output)
    assert retrieval.input == {"query": "What is the returns policy?"}
    assert retrieval.output[0]["content"].startswith("Returns are accepted")
    assert any(s.kind == "embedding" for s in sink.spans)
    # everything is one trace under the caller's span, and nesting follows LlamaIndex's
    assert {s.trace_id for s in sink.spans} == {root.trace_id}
    names, node = [], llm
    while node.parent_id in spans:
        node = spans[node.parent_id]
        names.append(node.name)
    assert names[-2:] == ["RetrieverQueryEngine.query", "request"]  # llm -> ... -> query -> app
    assert not mod._spans  # every span closed, nothing leaks


def test_streaming_call_is_recorded_without_breaking_the_stream(sink) -> None:
    llm = _llm()
    sink.spans.clear()
    assert "".join(c.delta for c in llm.stream_complete("hi")) == "Returns ok"
    s = next(s for s in sink.spans if s.kind == "llm")
    assert s.model == "gpt-4o-mini" and s.input == {"prompt": "hi"}


def test_errors_are_recorded(sink) -> None:
    from llama_index.core import Settings

    class Boom(type(_llm())):
        def complete(self, prompt, formatted=False, **kw):
            raise RuntimeError("model down")

    Settings.llm = Boom()
    with pytest.raises(RuntimeError):
        Settings.llm.complete("x")
    assert any(s.status == "error" and "model down" in (s.error or "") for s in sink.spans)


def test_instrument_twice_adds_handlers_once() -> None:
    import llama_index.core.instrumentation as instrumentation

    before = len(instrumentation.get_dispatcher().span_handlers)
    assert tirkit.instrument("llamaindex") == ["llamaindex"]
    assert len(instrumentation.get_dispatcher().span_handlers) == before
