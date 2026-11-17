"""Gemini (google-genai) auto-instrumentation against the real SDK with a mocked transport."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

genai = pytest.importorskip("google.genai")
httpx2 = pytest.importorskip("httpx2")

import tirkit  # noqa: E402
from tirkit.instrumentation import uninstrument  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 200

RESPONSE = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "A purple square."}]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 300,
        "candidatesTokenCount": 6,
        "thoughtsTokenCount": 40,
        "cachedContentTokenCount": 100,
        "totalTokenCount": 346,
    },
    "modelVersion": "gemini-2.5-flash",
}


def handler(request):
    path = request.url.path
    if path.endswith(":streamGenerateContent"):
        chunks = [
            {
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Hel"}]}}],
                "modelVersion": "gemini-2.5-flash",
            },
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "lo"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 5,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 7,
                },
            },
        ]
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
        return httpx2.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )
    if path.endswith(":generateContent"):
        body = json.loads(request.content)
        if "AUDIO" in json.dumps(body.get("generationConfig", {})):
            pcm = b"\0\0" * 24000  # 1 s of 24 kHz silence
            audio = {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "audio/L16;codec=pcm;rate=24000",
                                        "data": base64.b64encode(pcm).decode(),
                                    }
                                }
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 8,
                    "candidatesTokenCount": 25,
                    "totalTokenCount": 33,
                    "candidatesTokensDetails": [{"modality": "AUDIO", "tokenCount": 25}],
                },
                "modelVersion": "gemini-2.5-flash-preview-tts",
            }
            return httpx2.Response(200, json=audio)
        return httpx2.Response(200, json=RESPONSE)
    if path.endswith(":batchEmbedContents") or path.endswith(":embedContent"):
        return httpx2.Response(200, json={"embeddings": [{"values": [0.1, 0.2, 0.3, 0.4]}]})
    return httpx2.Response(404, json={"error": {"message": "no route", "code": 404}})


def make_client(async_: bool = False):
    from google.genai import types

    transport = httpx2.MockTransport(handler)
    if async_:
        opts = types.HttpOptions(httpx_async_client=httpx2.AsyncClient(transport=transport))
    else:
        opts = types.HttpOptions(httpx_client=httpx2.Client(transport=transport))
    return genai.Client(api_key="test-key", http_options=opts)


@pytest.fixture(autouse=True)
def _instrumented():
    tirkit.instrument("gemini")
    yield
    uninstrument()


def test_generate_content_with_image_records_tokens_and_media(sink) -> None:
    from google.genai import types

    client = make_client()
    res = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[types.Part.from_bytes(data=PNG, mime_type="image/png"), "What is this?"],
        config=types.GenerateContentConfig(
            temperature=0.1, max_output_tokens=200, system_instruction="Be brief."
        ),
    )
    assert res.text == "A purple square."
    s = sink.by_name("gemini.generate_content")
    assert (
        s.kind == "llm" and s.model == "gemini-2.5-flash" and s.attributes["provider"] == "gemini"
    )
    assert s.usage == {"input_tokens": 300, "output_tokens": 46, "cached_input_tokens": 100}
    assert s.attributes["reasoning_tokens"] == 40
    assert s.model_params == {"temperature": 0.1, "max_output_tokens": 200}
    assert s.input["system"] == "Be brief."
    image = s.input["contents"][0]["inline_data"]
    assert image.mime == "image/png" and image.data == PNG
    assert s.output["parts"][0]["text"] == "A purple square."
    assert s.cost_usd is not None and s.cost_usd > 0


def test_streaming(sink) -> None:
    client = make_client()
    text = "".join(
        c.text or ""
        for c in client.models.generate_content_stream(model="gemini-2.5-flash", contents="hi")
    )
    assert text == "Hello"
    s = sink.by_name("gemini.generate_content")
    assert s.output["parts"] == [{"text": "Hello"}] and s.usage["output_tokens"] == 2
    assert s.ttft_ms is not None and s.attributes["streaming"] is True


def test_text_to_speech_output_is_playable_audio(sink) -> None:
    from google.genai import types

    client = make_client()
    client.models.generate_content(
        model="gemini-2.5-flash-preview-tts",
        contents="Say hello",
        config=types.GenerateContentConfig(response_modalities=["AUDIO"]),
    )
    s = sink.by_name("gemini.generate_content")
    assert s.kind == "text_to_speech"
    audio = s.output["parts"][0]["inline_data"]
    assert audio.mime == "audio/wav" and audio.data[:4] == b"RIFF"
    assert s.usage["output_audio_tokens"] == 25


def test_async_and_embeddings(sink) -> None:
    client = make_client(async_=True)

    async def go() -> None:
        await client.aio.models.generate_content(model="gemini-2.5-flash", contents="x")

    asyncio.run(go())
    assert sink.by_name("gemini.generate_content").usage["input_tokens"] == 300
    make_client().models.embed_content(model="text-embedding-004", contents="hello")
    e = sink.by_name("gemini.embed_content")
    assert e.kind == "embedding" and e.output == {"count": 1, "dimensions": 4}
