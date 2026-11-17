"""Vertex AI adapter, against a fake ``vertexai`` package (the real one is large and needs
Google credentials). The fake mirrors the SDK's shapes: ``generate_content(contents, ...,
stream=...)`` and the ``usage_metadata`` / ``candidates`` response objects."""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace as NS

import pytest

import tirkit
from tirkit.instrumentation import uninstrument


class Part(NS):
    """Like ``vertexai`` types: objects with ``to_dict()``."""

    def to_dict(self):
        return {"text": self.text}


class Content(NS):
    def to_dict(self):
        return {"role": self.role, "parts": [p.to_dict() for p in self.parts]}


def _response(text: str, **usage):
    return NS(
        candidates=[
            NS(content=Content(role="model", parts=[Part(text=text)]), finish_reason="STOP")
        ],
        usage_metadata=NS(
            prompt_token_count=usage.get("i", 12),
            candidates_token_count=usage.get("o", 3),
            cached_content_token_count=usage.get("c"),
            thoughts_token_count=usage.get("t"),
        ),
    )


class GenerativeModel:
    def __init__(self, model_name: str) -> None:
        self._model_name = f"projects/p/locations/l/publishers/google/models/{model_name}"
        self.fail: BaseException | None = None

    def generate_content(self, contents, *, generation_config=None, tools=None, stream=False):
        if self.fail:
            raise self.fail
        if stream:
            return iter([_response("Par", i=None, o=None), _response("is", i=12, o=3)])
        return _response("Paris", c=4, t=2)

    async def generate_content_async(self, contents, *, generation_config=None, stream=False):
        if self.fail:
            raise self.fail
        if stream:

            async def gen():
                yield _response("ok")

            return gen()
        return _response("Paris")


@pytest.fixture
def fake_vertex(monkeypatch):
    pkg = types.ModuleType("vertexai")
    mod = types.ModuleType("vertexai.generative_models")
    mod.GenerativeModel = GenerativeModel
    monkeypatch.setitem(sys.modules, "vertexai", pkg)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", mod)
    assert tirkit.instrument("vertex") == ["vertex"]
    yield GenerativeModel
    uninstrument()


def test_generate_content_positional_contents_usage_and_cost(sink, fake_vertex) -> None:
    model = fake_vertex("gemini-2.0-flash")
    model.generate_content("Capital of France?", generation_config={"temperature": 0.1, "x": 1})
    s = sink.by_name("vertex.generate_content")
    assert s.kind == "llm" and s.model == "gemini-2.0-flash"
    assert s.attributes["provider"] == "vertex_ai"
    assert s.input["contents"] == "Capital of France?"
    assert s.output["parts"][0]["text"] == "Paris"
    assert s.model_params == {"temperature": 0.1}
    assert s.usage == {
        "input_tokens": 12,
        "output_tokens": 5,  # candidates + thinking, as on the Gen AI SDK path
        "cached_input_tokens": 4,
        "reasoning_tokens": 2,
    }
    assert s.cost_usd and s.cost_usd > 0


def test_streaming_sync_and_async(sink, fake_vertex) -> None:
    model = fake_vertex("gemini-2.0-flash")
    assert len(list(model.generate_content("x", stream=True))) == 2
    s = sink.spans[-1]
    assert [p["text"] for p in s.output["parts"]] == ["Par", "is"]
    assert s.usage == {"input_tokens": 12, "output_tokens": 3} and s.ttft_ms is not None

    async def run() -> None:
        stream = await model.generate_content_async("x", stream=True)
        async for _ in stream:
            pass
        await model.generate_content_async("y")

    asyncio.run(run())
    assert [s.name for s in sink.spans].count("vertex.generate_content") == 3
    assert sink.spans[-2].output["parts"][0]["text"] == "ok"


def test_errors_are_recorded_and_reraised(sink, fake_vertex) -> None:
    model = fake_vertex("gemini-2.0-flash")
    model.fail = RuntimeError("quota exceeded")
    with pytest.raises(RuntimeError):
        model.generate_content("x")
    s = sink.by_name("vertex.generate_content")
    assert s.status == "error" and "quota exceeded" in (s.error or "")


def test_noop_without_the_sdk(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "vertexai", None)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", None)
    assert tirkit.instrument("vertex") == []
