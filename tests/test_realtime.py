"""OpenAI Realtime (speech-to-speech) tracing."""

from __future__ import annotations

import asyncio
import base64

import pytest

from tirkit.instrumentation.realtime import RealtimeTracer, wrap_connection_class
from tirkit.tracing import span

PCM_1S = b"\x01\x00" * 24000  # 1 s of 24 kHz 16-bit audio


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def voice_turn(t: RealtimeTracer) -> None:
    t.on_server_event(
        {"type": "session.created", "session": {"model": "gpt-realtime", "voice": "alloy"}}
    )
    t.on_client_event(
        {"type": "session.update", "session": {"instructions": "Be brief.", "voice": "alloy"}}
    )
    for _ in range(2):
        t.on_client_event({"type": "input_audio_buffer.append", "audio": b64(PCM_1S[:24000])})
    t.on_server_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "Where is my order?",
        }
    )
    t.on_server_event({"type": "response.created", "response": {"id": "r1"}})
    t.on_server_event({"type": "response.output_audio.delta", "delta": b64(PCM_1S[:24000])})
    t.on_server_event({"type": "response.output_audio.delta", "delta": b64(PCM_1S[24000:])})
    t.on_server_event({"type": "response.output_audio_transcript.delta", "delta": "It shipped "})
    t.on_server_event({"type": "response.output_audio_transcript.delta", "delta": "yesterday."})
    t.on_server_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "lookup",
            "call_id": "c1",
            "arguments": "{}",
        }
    )
    t.on_server_event(
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 80,
                    "input_token_details": {"audio_tokens": 100, "cached_tokens": 20},
                    "output_token_details": {"audio_tokens": 70},
                },
            },
        }
    )


def test_voice_turn_becomes_speech_to_speech_span(sink) -> None:
    t = RealtimeTracer()
    with span("call-centre") as root:
        voice_turn(t)
    t.close()
    turn = sink.by_name("openai.realtime.response")
    session = sink.by_name("openai.realtime.session")
    assert turn.kind == "speech_to_speech" and turn.model == "gpt-realtime"
    assert turn.parent_id == session.span_id and session.parent_id == root.span_id
    assert (
        turn.input["transcript"] == "Where is my order?"
        and turn.input["instructions"] == "Be brief."
    )
    assert turn.input["audio"].data[:4] == b"RIFF" and turn.attributes[
        "input_audio_seconds"
    ] == pytest.approx(1.0)
    assert turn.output["transcript"] == "It shipped yesterday."
    assert turn.output["audio"].mime == "audio/wav" and turn.attributes[
        "output_audio_seconds"
    ] == pytest.approx(1.0)
    assert turn.output["tool_calls"][0]["name"] == "lookup"
    assert turn.usage == {
        "input_tokens": 120,
        "output_tokens": 80,
        "cached_input_tokens": 20,
        "input_audio_tokens": 100,
        "output_audio_tokens": 70,
    }
    assert turn.cost_usd is not None and turn.cost_usd > 0
    assert turn.ttft_ms is not None and turn.model_params == {"voice": "alloy"}


def test_text_turns_failures_and_close_mid_response(sink) -> None:
    t = RealtimeTracer("gpt-realtime")
    t.on_client_event(
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        }
    )
    t.on_client_event({"type": "response.create"})
    t.on_server_event({"type": "response.output_text.delta", "delta": "hello"})
    t.on_server_event(
        {
            "type": "response.done",
            "response": {"status": "failed", "status_details": {"reason": "x"}},
        }
    )
    t.on_client_event({"type": "response.create"})
    t.on_server_event(
        {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    )
    t.close()
    first, second, session = sink.spans
    assert first.kind == "llm" and first.output == {"text": "hello"} and first.status == "error"
    assert second.status == "error" and "closed" in second.error
    assert second.attributes["errors"][0]["message"] == "bad"
    assert session.name == "openai.realtime.session"


def test_patched_connection_classes_sync_and_async(sink) -> None:
    class FakeConn:
        def __init__(self, events):
            self.events = list(events)
            self.sent = []

        def send(self, event):
            self.sent.append(event)

        def recv(self):
            return self.events.pop(0)

        def close(self):
            return None

    class FakeAsyncConn:
        def __init__(self, events):
            self.events = list(events)

        async def send(self, event):
            return None

        async def recv(self):
            return self.events.pop(0)

        async def close(self):
            return None

    events = [
        {"type": "response.created"},
        {"type": "response.output_text.delta", "delta": "ok"},
        {"type": "response.done", "response": {"status": "completed"}},
    ]
    wrap_connection_class(FakeConn, is_async=False)
    wrap_connection_class(FakeAsyncConn, is_async=True)
    c = FakeConn(events)
    c.send({"type": "response.create"})
    for _ in range(3):
        c.recv()
    c.close()

    async def go() -> None:
        a = FakeAsyncConn(events)
        await a.send({"type": "response.create"})
        for _ in range(3):
            await a.recv()
        await a.close()

    asyncio.run(go())
    responses = [s for s in sink.spans if s.name == "openai.realtime.response"]
    assert len(responses) == 2 and all(s.output == {"text": "ok"} for s in responses)


def test_instrument_patches_the_real_sdk_classes() -> None:
    pytest.importorskip("openai")
    from openai.resources.realtime import realtime

    import tirkit
    from tirkit.instrumentation import uninstrument

    tirkit.instrument("openai")
    try:
        assert getattr(realtime.RealtimeConnection.recv, "__tirkit_wrapped__", False)
        assert getattr(realtime.AsyncRealtimeConnection.send, "__tirkit_wrapped__", False)
    finally:
        uninstrument()
