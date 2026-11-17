"""Provider auto-instrumentation against the real SDKs with a mocked HTTP transport."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import struct
import wave
from typing import Any

import pytest

from tests.conftest import sse

openai = pytest.importorskip("openai")
anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

import tirkit  # noqa: E402
from tirkit.instrumentation import uninstrument  # noqa: E402


def wav_bytes(seconds: float = 1.0, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(seconds * rate))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _instrumented():
    tirkit.instrument()
    yield
    uninstrument()


def responder(routes: dict[str, Any]):
    def handle(request: Any) -> Any:
        for path, resp in routes.items():
            if request.url.path.endswith(path):
                body, ctype = resp if isinstance(resp, tuple) else (resp, "application/json")
                if isinstance(body, dict):
                    body = json.dumps(body).encode()
                return httpx2.Response(200, content=body, headers={"content-type": ctype})
        return httpx2.Response(404, json={"error": {"message": "no route"}})

    return handle


def oai(
    routes: dict[str, Any], *, async_: bool = False, base_url: str = "https://api.openai.com/v1"
):
    transport = httpx2.MockTransport(responder(routes))
    if async_:
        return openai.AsyncOpenAI(
            api_key="k", base_url=base_url, http_client=httpx2.AsyncClient(transport=transport)
        )
    return openai.OpenAI(
        api_key="k", base_url=base_url, http_client=httpx2.Client(transport=transport)
    )


def ant(routes: dict[str, Any], *, async_: bool = False):
    transport = httpx2.MockTransport(responder(routes))
    if async_:
        return anthropic.AsyncAnthropic(
            api_key="k", http_client=httpx2.AsyncClient(transport=transport)
        )
    return anthropic.Anthropic(api_key="k", http_client=httpx2.Client(transport=transport))


CHAT = {
    "id": "c1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-4o-mini-2024-07-18",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Paris"}}
    ],
    "usage": {
        "prompt_tokens": 20,
        "completion_tokens": 2,
        "total_tokens": 22,
        "prompt_tokens_details": {"cached_tokens": 10},
    },
}


def test_openai_chat_with_vision_input(sink) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 2000
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    client = oai({"/chat/completions": CHAT})
    with tirkit.span("app") as root:
        out = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "where?"},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ],
            temperature=0.1,
        )
    assert out.choices[0].message.content == "Paris"
    s = sink.by_name("openai.chat")
    assert s.parent_id == root.span_id and s.kind == "llm"
    assert s.model == "gpt-4o-mini-2024-07-18"
    assert s.output["content"] == "Paris"
    assert s.usage == {"input_tokens": 20, "output_tokens": 2, "cached_input_tokens": 10}
    assert s.cost_usd and s.cost_usd > 0
    assert s.model_params == {"temperature": 0.1}
    assert s.attributes["provider"] == "openai"


def test_openai_chat_streaming_with_tool_call(sink) -> None:
    chunks = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"content": "lo"}}],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "t1",
                                "type": "function",
                                "function": {"name": "f", "arguments": '{"a":'},
                            }
                        ]
                    },
                }
            ],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]},
                    "finish_reason": "tool_calls",
                }
            ],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-4o",
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        },
    ]
    client = oai({"/chat/completions": (sse(chunks, done=True), "text/event-stream")})
    stream = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    text = "".join(c.choices[0].delta.content or "" for c in stream if c.choices)
    assert text == "Hello"
    s = sink.by_name("openai.chat")
    assert s.output["content"] == "Hello"
    assert s.output["tool_calls"] == [{"id": "t1", "name": "f", "arguments": '{"a":1}'}]
    assert s.usage["output_tokens"] == 7
    assert s.ttft_ms is not None


def test_openai_compatible_local_model(sink) -> None:
    client = oai(
        {"/chat/completions": {**CHAT, "model": "llama3.2:1b"}},
        base_url="http://localhost:11434/v1",
    )
    client.chat.completions.create(
        model="llama3.2:1b", messages=[{"role": "user", "content": "hi"}]
    )
    s = sink.by_name("openai.chat")
    assert s.attributes["provider"] == "openai-compatible"
    assert s.attributes["provider_host"] == "localhost"
    assert s.model == "llama3.2:1b"


def test_openai_async_chat(sink) -> None:
    client = oai({"/chat/completions": CHAT}, async_=True)

    async def go() -> None:
        await client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]
        )

    asyncio.run(go())
    assert sink.by_name("openai.chat").output["content"] == "Paris"


def test_openai_embeddings(sink) -> None:
    client = oai(
        {
            "/embeddings": {
                "object": "list",
                "model": "text-embedding-3-small",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            }
        }
    )
    client.embeddings.create(model="text-embedding-3-small", input="hello")
    s = sink.by_name("openai.embeddings")
    assert s.kind == "embedding" and s.output == {"count": 1, "dimensions": 3}
    assert s.usage == {"input_tokens": 4}


def test_openai_speech_to_text(sink) -> None:
    audio = wav_bytes(2.0)
    client = oai(
        {
            "/audio/transcriptions": {
                "text": "hello world",
                "usage": {"type": "duration", "seconds": 2},
            }
        }
    )
    out = client.audio.transcriptions.create(model="whisper-1", file=("a.wav", audio, "audio/wav"))
    assert out.text == "hello world"
    s = sink.by_name("openai.transcription")
    assert s.kind == "speech_to_text"
    assert s.output == {"text": "hello world"}
    assert s.usage["input_audio_seconds"] == 2
    assert s.attributes["real_time_factor"] >= 0
    assert s.input["audio"].data == audio  # stored by the exporter as media
    assert s.cost_usd == pytest.approx(2 * 0.0001)


def test_openai_text_to_speech(sink) -> None:
    audio = wav_bytes(1.5)
    client = oai({"/audio/speech": (audio, "audio/wav")})
    resp = client.audio.speech.create(
        model="tts-1", voice="alloy", input="Hello there", response_format="wav"
    )
    assert resp.content == audio
    s = sink.by_name("openai.speech")
    assert s.kind == "text_to_speech"
    assert s.input == {"text": "Hello there", "voice": "alloy"}
    assert s.usage["input_characters"] == 11
    assert s.usage["output_audio_seconds"] == pytest.approx(1.5)
    assert s.output["audio"].data == audio
    assert s.cost_usd == pytest.approx(11 * 1.5e-5)


def test_openai_image_generation(sink) -> None:
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 100).decode()
    client = oai({"/images/generations": {"created": 0, "data": [{"b64_json": png}]}})
    client.images.generate(model="gpt-image-1", prompt="a cat")
    s = sink.by_name("openai.images")
    assert s.kind == "image_generation" and s.usage["output_images"] == 1
    assert s.output[0]["b64_json"] == png


def test_openai_error_is_recorded(sink) -> None:
    client = oai({})
    with pytest.raises(openai.NotFoundError):
        client.with_options(max_retries=0).chat.completions.create(model="gpt-4o", messages=[])
    s = sink.by_name("openai.chat")
    assert s.status == "error" and "NotFoundError" in s.error


MSG = {
    "id": "m1",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5-5",
    "content": [{"type": "text", "text": "Bonjour"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 20,
    },
}


def test_anthropic_messages_with_cache_usage(sink) -> None:
    client = ant({"/v1/messages": MSG})
    client.messages.create(
        model="claude-opus-5-5",
        max_tokens=100,
        system="be brief",
        messages=[{"role": "user", "content": "hi"}],
    )
    s = sink.by_name("anthropic.messages")
    assert s.output == [{"type": "text", "text": "Bonjour"}]
    assert s.input["system"] == "be brief"
    assert s.usage == {
        "input_tokens": 130,
        "cached_input_tokens": 100,
        "cache_write_tokens": 20,
        "output_tokens": 4,
    }
    assert s.cost_usd == pytest.approx(10 * 4e-6 + 100 * 2e-7 + 20 * 5e-6 + 4 * 2e-5)
    assert s.attributes["stop_reason"] == "end_turn"
    assert s.model_params == {"max_tokens": 100}


ANT_EVENTS = [
    {
        "type": "message_start",
        "message": {
            "id": "m2",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi "}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "there"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "id": "tu1", "name": "lookup", "input": {}},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"q": '},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '"x"}'},
    },
    {"type": "content_block_stop", "index": 1},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": 9},
    },
    {"type": "message_stop"},
]


def test_anthropic_raw_stream(sink) -> None:
    client = ant({"/v1/messages": (sse(ANT_EVENTS, named=True), "text/event-stream")})
    events = list(
        client.messages.create(
            model="claude-opus-5-5",
            max_tokens=50,
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
    )
    assert events[-1].type == "message_stop"
    s = sink.by_name("anthropic.messages")
    assert s.output[0]["text"] == "Hi there"
    assert s.output[1]["input"] == {"q": "x"}
    assert s.usage == {"input_tokens": 12, "output_tokens": 9}
    assert s.attributes["stop_reason"] == "tool_use"
    assert s.ttft_ms is not None


def test_anthropic_stream_helper(sink) -> None:
    client = ant({"/v1/messages": (sse(ANT_EVENTS, named=True), "text/event-stream")})
    with client.messages.stream(
        model="claude-opus-5-5", max_tokens=50, messages=[{"role": "user", "content": "hi"}]
    ) as stream:
        text = "".join(stream.text_stream)
    assert text == "Hi there"
    s = sink.by_name("anthropic.messages")
    assert s.output[0]["text"] == "Hi there"
    assert s.usage["output_tokens"] == 9


def test_anthropic_async(sink) -> None:
    client = ant({"/v1/messages": MSG}, async_=True)

    async def go() -> None:
        await client.messages.create(
            model="claude-opus-5-5", max_tokens=10, messages=[{"role": "user", "content": "x"}]
        )

    asyncio.run(go())
    assert sink.by_name("anthropic.messages").output[0]["text"] == "Bonjour"


def test_uninstrument_restores_methods(sink) -> None:
    uninstrument()
    client = oai({"/chat/completions": CHAT})
    client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}])
    assert sink.spans == []
