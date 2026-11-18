"""Cohere adapter: real SDK (v2 client), mocked HTTP transport."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

cohere = pytest.importorskip("cohere")
httpx = pytest.importorskip("httpx")

import tirkit  # noqa: E402
from tirkit.instrumentation import uninstrument  # noqa: E402

CHAT = {
    "id": "c1",
    "finish_reason": "COMPLETE",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "Paris"}]},
    "usage": {
        "billed_units": {"input_tokens": 20, "output_tokens": 2},
        "tokens": {"input_tokens": 25, "output_tokens": 2},
    },
}


@pytest.fixture(autouse=True)
def _instrumented():
    assert tirkit.instrument("cohere") == ["cohere"]
    yield
    uninstrument()


def client(handler):
    return cohere.ClientV2(
        api_key="k", httpx_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def aclient(handler):
    return cohere.AsyncClientV2(
        api_key="k", httpx_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def events(*items: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in items)


STREAM = events(
    {"type": "message-start", "id": "s1", "delta": {"message": {"role": "assistant"}}},
    {"type": "content-delta", "index": 0, "delta": {"message": {"content": {"text": "Par"}}}},
    {"type": "content-delta", "index": 0, "delta": {"message": {"content": {"text": "is"}}}},
    {
        "type": "message-end",
        "delta": {
            "finish_reason": "COMPLETE",
            "usage": {"billed_units": {"input_tokens": 9, "output_tokens": 3}},
        },
    },
)


def test_chat_records_billed_usage_and_cost(sink) -> None:
    out = client(lambda r: httpx.Response(200, json=CHAT)).chat(
        model="command-r-08-2024",
        messages=[{"role": "user", "content": "Capital of France?"}],
        temperature=0.2,
    )
    assert out.message.content[0].text == "Paris"
    s = sink.by_name("cohere.chat")
    assert s.kind == "llm" and s.attributes["provider"] == "cohere"
    assert s.input["messages"][0]["content"] == "Capital of France?"
    assert s.output["content"][0]["text"] == "Paris"
    assert s.usage == {"input_tokens": 20, "output_tokens": 2}  # billed units
    assert s.model_params == {"temperature": 0.2}
    assert s.model == "command-r-08-2024" and s.cost_usd and s.cost_usd > 0


def test_chat_stream(sink) -> None:
    c = client(
        lambda r: httpx.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})
    )
    text = "".join(
        e.delta.message.content.text
        for e in c.chat_stream(
            model="command-r-08-2024", messages=[{"role": "user", "content": "x"}]
        )
        if e.type == "content-delta"
    )
    assert text == "Paris"
    s = sink.by_name("cohere.chat")
    assert s.output == {"role": "assistant", "content": "Paris"}
    assert s.usage == {"input_tokens": 9, "output_tokens": 3} and s.ttft_ms is not None
    assert s.attributes["response_id"] == "s1"


def test_async_chat_and_stream(sink) -> None:
    async def run() -> None:
        await aclient(lambda r: httpx.Response(200, json=CHAT)).chat(
            model="command-r-08-2024", messages=[{"role": "user", "content": "x"}]
        )
        c = aclient(
            lambda r: httpx.Response(
                200, content=STREAM, headers={"content-type": "text/event-stream"}
            )
        )
        async for _ in c.chat_stream(
            model="command-r-08-2024", messages=[{"role": "user", "content": "x"}]
        ):
            pass

    asyncio.run(run())
    first, second = [s for s in sink.spans if s.name == "cohere.chat"]
    assert first.usage["input_tokens"] == 20 and second.output["content"] == "Paris"


def test_error_is_recorded_and_reraised(sink) -> None:
    c = client(lambda r: httpx.Response(429, json={"message": "rate limited"}))
    with pytest.raises(cohere.errors.TooManyRequestsError):
        c.chat(model="command-r-08-2024", messages=[{"role": "user", "content": "x"}])
    s = sink.by_name("cohere.chat")
    assert s.status == "error" and "TooManyRequests" in (s.error or "")


def test_rerank_and_embed(sink) -> None:
    rr = {
        "id": "r",
        "results": [{"index": 1, "relevance_score": 0.9}],
        "meta": {"billed_units": {"search_units": 1}},
    }
    client(lambda r: httpx.Response(200, json=rr)).rerank(
        model="rerank-v3.5", query="q", documents=["a", "b"]
    )
    s = sink.by_name("cohere.rerank")
    assert s.kind == "rerank" and s.usage == {"requests": 1.0}
    assert s.output == [{"index": 1, "score": 0.9}]
    emb = {
        "id": "e",
        "embeddings": {"float": [[0.1, 0.2]]},
        "texts": ["a"],
        "meta": {"billed_units": {"input_tokens": 3}},
    }
    client(lambda r: httpx.Response(200, json=emb)).embed(
        model="embed-v4.0", input_type="search_document", texts=["a"], embedding_types=["float"]
    )
    e = sink.by_name("cohere.embed")
    assert (
        e.kind == "embedding" and e.usage == {"input_tokens": 3} and e.output == {"embeddings": 1}
    )


def test_noop_without_the_sdk(monkeypatch) -> None:
    uninstrument()
    for name in list(sys.modules):
        if name == "cohere" or name.startswith("cohere."):
            monkeypatch.setitem(sys.modules, name, None)
    assert tirkit.instrument("cohere") == []
