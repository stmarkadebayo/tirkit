"""Mistral adapter: real SDK, mocked HTTP transport."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from tests.conftest import sse

mistral = pytest.importorskip("mistralai.client")
httpx = pytest.importorskip("httpx")

import tirkit  # noqa: E402
from tirkit.instrumentation import uninstrument  # noqa: E402

CHAT = {
    "id": "m1",
    "object": "chat.completion",
    "created": 0,
    "model": "mistral-small-latest",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Paris"}}
    ],
    "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
}


@pytest.fixture(autouse=True)
def _instrumented():
    assert tirkit.instrument("mistral") == ["mistral"]
    yield
    uninstrument()


def client(handler, *, async_: bool = False):
    transport = httpx.MockTransport(handler)
    if async_:
        return mistral.Mistral(api_key="k", async_client=httpx.AsyncClient(transport=transport))
    return mistral.Mistral(api_key="k", client=httpx.Client(transport=transport))


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=CHAT)


def test_complete_records_input_output_usage_and_cost(sink) -> None:
    out = client(ok).chat.complete(
        model="mistral-small-latest",
        messages=[{"role": "user", "content": "Capital of France?"}],
        temperature=0.1,
    )
    assert out.choices[0].message.content == "Paris"
    s = sink.by_name("mistral.chat")
    assert s.kind == "llm" and s.attributes["provider"] == "mistral"
    assert s.input["messages"][0]["content"] == "Capital of France?"
    assert s.output["content"] == "Paris"
    assert s.usage == {"input_tokens": 20, "output_tokens": 2}
    assert s.model_params == {"temperature": 0.1}
    assert s.model == "mistral-small-latest" and s.cost_usd and s.cost_usd > 0


def test_stream_collects_text_and_usage(sink) -> None:
    chunks = [
        {
            "id": "c",
            "model": "mistral-small-latest",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "Par"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "c",
            "model": "mistral-small-latest",
            "choices": [{"index": 0, "delta": {"content": "is"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        },
    ]
    body = sse(chunks, done=True)
    c = client(
        lambda r: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    )
    stream = c.chat.stream(
        model="mistral-small-latest", messages=[{"role": "user", "content": "x"}]
    )
    text = "".join(e.data.choices[0].delta.content or "" for e in stream)
    assert text == "Paris"
    s = sink.by_name("mistral.chat")
    assert s.output["content"] == "Paris" and s.usage == {"input_tokens": 5, "output_tokens": 2}
    assert s.ttft_ms is not None


def test_async_complete_and_stream(sink) -> None:
    body = sse(
        [
            {
                "id": "c",
                "model": "m",
                "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
            }
        ],
        done=True,
    )

    async def run() -> None:
        c = client(ok, async_=True)
        await c.chat.complete_async(
            model="mistral-small-latest", messages=[{"role": "user", "content": "x"}]
        )
        c2 = client(
            lambda r: httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            ),
            async_=True,
        )
        stream = await c2.chat.stream_async(
            model="mistral-small-latest", messages=[{"role": "user", "content": "x"}]
        )
        async for _ in stream:
            pass

    asyncio.run(run())
    spans = [s for s in sink.spans if s.name == "mistral.chat"]
    assert len(spans) == 2 and spans[1].output["content"] == "ok"


def test_error_is_recorded_and_reraised(sink) -> None:
    c = client(lambda r: httpx.Response(429, json={"message": "rate limited"}))
    with pytest.raises(Exception):  # noqa: B017, PT011 - the SDK's own error type
        c.chat.complete(
            model="mistral-small-latest", messages=[{"role": "user", "content": "x"}], retries=None
        )
    s = sink.by_name("mistral.chat")
    assert s.status == "error" and "429" in (s.error or "")


def test_embeddings(sink) -> None:
    emb = {
        "id": "e",
        "object": "list",
        "model": "mistral-embed",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }
    c = client(lambda r: httpx.Response(200, json=emb))
    c.embeddings.create(model="mistral-embed", inputs=["hi"])
    s = sink.by_name("mistral.embeddings")
    assert (
        s.kind == "embedding" and s.usage == {"input_tokens": 4} and s.output == {"embeddings": 1}
    )


def test_noop_without_the_sdk(monkeypatch) -> None:
    uninstrument()
    for name in list(sys.modules):
        if name.startswith("mistralai"):
            monkeypatch.setitem(sys.modules, name, None)
    assert tirkit.instrument("mistral") == []


def test_unwrapped_calls_still_work_when_tracing_breaks(monkeypatch, sink) -> None:
    import tirkit.instrumentation._patch as p

    monkeypatch.setattr(p, "start_span", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert client(ok).chat.complete(model="m", messages=[{"role": "user", "content": "x"}]).choices
    assert json.dumps(sink.spans) == "[]"
