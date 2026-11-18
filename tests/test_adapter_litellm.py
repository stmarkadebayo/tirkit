"""LiteLLM adapter: real library with ``mock_response`` (no network)."""

from __future__ import annotations

import asyncio
import sys

import pytest

litellm = pytest.importorskip("litellm")

import tirkit  # noqa: E402
from tirkit.instrumentation import uninstrument  # noqa: E402


@pytest.fixture(autouse=True)
def _instrumented():
    assert tirkit.instrument("litellm") == ["litellm"]
    yield
    uninstrument()


MSG = [{"role": "user", "content": "Capital of France?"}]


def test_completion_records_output_usage_provider_and_cost(sink) -> None:
    out = litellm.completion(
        model="gpt-4o-mini", messages=MSG, mock_response="Paris", temperature=0.1
    )
    assert out.choices[0].message.content == "Paris"
    s = sink.by_name("litellm.completion")
    assert s.kind == "llm" and s.input["messages"] == MSG
    assert s.output["content"] == "Paris"
    assert s.usage["input_tokens"] > 0 and s.usage["output_tokens"] > 0
    assert s.model_params == {"temperature": 0.1} and s.attributes["via"] == "litellm"
    assert s.cost_usd and s.cost_usd > 0
    assert "mock_response" not in s.model_params


def test_provider_prefix_is_recorded(sink) -> None:
    litellm.completion(model="openai/gpt-4o-mini", messages=MSG, mock_response="ok")
    assert sink.by_name("litellm.completion").attributes["provider"] == "openai"


def test_streaming_sync_and_async(sink) -> None:
    text = "".join(
        c.choices[0].delta.content or ""
        for c in litellm.completion(
            model="gpt-4o-mini", messages=MSG, mock_response="Paris is it", stream=True
        )
    )
    assert text.strip() == "Paris is it"
    s = sink.spans[-1]
    assert s.output["content"].strip() == "Paris is it" and s.ttft_ms is not None

    async def run() -> str:
        stream = await litellm.acompletion(
            model="gpt-4o-mini", messages=MSG, mock_response="Lyon", stream=True
        )
        return "".join([c.choices[0].delta.content or "" async for c in stream])

    assert asyncio.run(run()).strip() == "Lyon"
    assert sink.spans[-1].output["content"].strip() == "Lyon"


def test_async_completion(sink) -> None:
    out = asyncio.run(litellm.acompletion(model="gpt-4o-mini", messages=MSG, mock_response="Hi"))
    assert out.choices[0].message.content == "Hi"
    assert sink.by_name("litellm.completion").output["content"] == "Hi"


def test_errors_are_recorded_and_reraised(sink) -> None:
    with pytest.raises(Exception, match="boom"):
        litellm.completion(
            model="gpt-4o-mini", messages=MSG, mock_response=Exception("boom"), num_retries=0
        )
    s = sink.by_name("litellm.completion")
    assert s.status == "error" and "boom" in (s.error or "")


def test_embedding(sink) -> None:
    litellm.embedding(model="text-embedding-3-small", input=["a"], mock_response=[[0.1, 0.2]])
    s = sink.by_name("litellm.embedding")
    assert s.kind == "embedding" and s.output == {"embeddings": 1}


def test_inner_sdk_spans_are_not_recorded_twice(sink, monkeypatch) -> None:
    """With the OpenAI SDK also instrumented, one LiteLLM call is still one span."""
    assert "openai" in tirkit.instrument("openai")
    openai = pytest.importorskip("openai")
    httpx2 = pytest.importorskip("httpx2")
    chat = {
        "id": "c",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o-mini",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "x"}}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }
    client = openai.OpenAI(
        api_key="k",
        http_client=httpx2.Client(
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=chat))
        ),
    )

    def fake_completion(*args, **kwargs):  # stands in for LiteLLM calling the provider SDK
        client.chat.completions.create(model="gpt-4o-mini", messages=MSG)
        return litellm.ModelResponse(
            model="gpt-4o-mini",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "x"},
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        )

    uninstrument()
    monkeypatch.setattr(litellm, "completion", fake_completion)
    tirkit.instrument("litellm", "openai")
    litellm.completion(model="gpt-4o-mini", messages=MSG)
    assert [s.name for s in sink.spans] == ["litellm.completion"]


def test_noop_without_the_library(monkeypatch) -> None:
    uninstrument()
    for name in list(sys.modules):
        if name == "litellm" or name.startswith("litellm."):
            monkeypatch.setitem(sys.modules, name, None)
    assert tirkit.instrument("litellm") == []
