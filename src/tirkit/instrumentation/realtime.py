"""OpenAI Realtime API (speech-to-speech voice agents) over WebSockets.

``tirkit.instrument("openai")`` also patches the SDK's realtime connections
(``client.realtime.connect(...)``, sync and async). Each connection becomes an
``openai.realtime.session`` span; each model response becomes a child span of kind
``speech_to_speech`` (or ``llm`` for text-only turns) with:

- input: the user's audio for the turn (``input_audio_buffer.append`` chunks, as WAV), its
  transcript when input transcription is on, and any text items sent;
- output: the spoken reply as WAV, its transcript, text and tool calls;
- usage from ``response.done`` (text and audio tokens, cached tokens) and time to first audio;
- status ``error`` for failed or cancelled responses and ``error`` events.

Audio is assumed to be 16-bit PCM at 24 kHz (the API default); other formats are stored raw.
"""

from __future__ import annotations

import base64
import io
import time
import wave
from typing import Any

from tirkit.media import Attachment
from tirkit.serialize import to_jsonable
from tirkit.tracing import Span, current_span, finish_span, start_span

MAX_AUDIO_BYTES = 25 * 1024 * 1024


def _wav(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _event(e: Any) -> dict[str, Any]:
    data = to_jsonable(e)
    return data if isinstance(data, dict) else {}


class RealtimeTracer:
    """Turns a stream of realtime client/server events into spans."""

    def __init__(self, model: str | None = None) -> None:
        self.model = model
        self.session: Span | None = None
        self.turn: Span | None = None
        self.config: dict[str, Any] = {}
        self.pcm_in = bytearray()
        self.pcm_out = bytearray()
        self.input_items: list[Any] = []
        self.input_transcript = ""
        self.out_transcript = ""
        self.out_text = ""
        self.tool_calls: list[dict[str, Any]] = []
        self.turn_started_at: float | None = None
        self.rate = 24000
        self.pcm = True

    # -- lifecycle -----------------------------------------------------------------------------

    def _ensure_session(self) -> Span:
        if self.session is None:
            self.session = start_span(
                "openai.realtime.session", parent=current_span(), kind="agent", model=self.model,
                provider="openai", api="realtime",
            )  # fmt: skip
        return self.session

    def _start_turn(self) -> Span:
        if self.turn is None:
            session = self._ensure_session()
            self.turn = start_span(
                "openai.realtime.response", parent=session, kind="speech_to_speech",
                model=self.model, provider="openai", api="realtime",
            )  # fmt: skip
            if self.turn_started_at is not None:
                self.turn.start = self.turn_started_at
            self.turn.model_params = {
                k: v
                for k, v in self.config.items()
                if k in {"voice", "temperature", "max_response_output_tokens", "output_modalities",
                         "modalities", "turn_detection"}
            }  # fmt: skip
        return self.turn

    def _audio(self, pcm: bytearray) -> Attachment | None:
        if not pcm:
            return None
        if self.pcm:
            return Attachment(data=_wav(bytes(pcm), self.rate), mime="audio/wav")
        return Attachment(data=bytes(pcm), mime="application/octet-stream")

    def _finish_turn(self, response: dict[str, Any] | None) -> None:
        s = self._start_turn()
        inp: dict[str, Any] = {}
        audio_in = self._audio(self.pcm_in)
        if audio_in is not None:
            inp["audio"] = audio_in
        if self.input_transcript:
            inp["transcript"] = self.input_transcript
        if self.input_items:
            inp["items"] = list(self.input_items)
        if self.config.get("instructions"):
            inp["instructions"] = self.config["instructions"]
        s.input = inp
        out: dict[str, Any] = {}
        audio_out = self._audio(self.pcm_out)
        if audio_out is not None:
            out["audio"] = audio_out
        if self.out_transcript:
            out["transcript"] = self.out_transcript
        if self.out_text:
            out["text"] = self.out_text
        if self.tool_calls:
            out["tool_calls"] = list(self.tool_calls)
        s.output = out
        if audio_in is None and audio_out is None:
            s.kind = "llm"
        response = response or {}
        s.model = response.get("model") or s.model
        status = response.get("status")
        if status in ("failed", "cancelled", "incomplete"):
            s.record_error(f"response {status}: {response.get('status_details')}")
            if status == "cancelled":
                s.level = "warning"
        u = response.get("usage") or {}
        if u:
            it = u.get("input_token_details") or {}
            ot = u.get("output_token_details") or {}
            s.set_usage(
                input_tokens=u.get("input_tokens"),
                output_tokens=u.get("output_tokens"),
                cached_input_tokens=it.get("cached_tokens"),
                input_audio_tokens=it.get("audio_tokens"),
                output_audio_tokens=ot.get("audio_tokens"),
            )
        if self.pcm and self.pcm_out:
            s.attributes["output_audio_seconds"] = len(self.pcm_out) / 2 / self.rate
        if self.pcm and self.pcm_in:
            s.attributes["input_audio_seconds"] = len(self.pcm_in) / 2 / self.rate
        finish_span(s)
        self.turn = None
        self.turn_started_at = None
        self.pcm_in.clear()
        self.pcm_out.clear()
        self.input_items.clear()
        self.input_transcript = self.out_transcript = self.out_text = ""
        self.tool_calls = []

    def close(self) -> None:
        if self.turn is not None:
            self.turn.record_error("connection closed during a response")
            finish_span(self.turn)
            self.turn = None
        if self.session is not None:
            finish_span(self.session)
            self.session = None

    # -- events ------------------------------------------------------------------------------------

    def _session_config(self, session: dict[str, Any]) -> None:
        self.config.update({k: v for k, v in session.items() if v is not None})
        self.model = session.get("model") or self.model
        fmt = ((session.get("audio") or {}).get("input") or {}).get("format") or session.get(
            "input_audio_format"
        )
        if isinstance(fmt, dict):
            self.pcm = str(fmt.get("type", "audio/pcm")) in ("audio/pcm", "pcm16")
            self.rate = int(fmt.get("rate") or self.rate)
        elif isinstance(fmt, str):
            self.pcm = fmt in ("pcm16", "audio/pcm")
        if self.session is not None and self.model:
            self.session.model = self.model

    def on_client_event(self, event: Any) -> None:
        e = _event(event)
        etype = e.get("type", "")
        if etype == "session.update":
            self._session_config(e.get("session") or {})
            self._ensure_session()
        elif etype == "input_audio_buffer.append":
            if len(self.pcm_in) < MAX_AUDIO_BYTES:
                try:
                    self.pcm_in.extend(base64.b64decode(e.get("audio") or ""))
                except ValueError:
                    pass
            if self.turn_started_at is None:
                self.turn_started_at = time.time()
        elif etype == "conversation.item.create":
            self.input_items.append(e.get("item"))
        elif etype == "response.create":
            self._start_turn()

    def on_server_event(self, event: Any) -> None:
        e = _event(event)
        etype = e.get("type", "")
        if etype in ("session.created", "session.updated"):
            self._session_config(e.get("session") or {})
            self._ensure_session()
        elif etype in ("response.created",):
            self._start_turn()
        elif etype in ("response.output_audio.delta", "response.audio.delta"):
            s = self._start_turn()
            if s.ttft_ms is None:
                s.ttft_ms = (time.time() - s.start) * 1000
            if len(self.pcm_out) < MAX_AUDIO_BYTES:
                try:
                    self.pcm_out.extend(base64.b64decode(e.get("delta") or ""))
                except ValueError:
                    pass
        elif etype in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
            self.out_transcript += e.get("delta") or ""
        elif etype in ("response.output_text.delta", "response.text.delta"):
            s = self._start_turn()
            if s.ttft_ms is None:
                s.ttft_ms = (time.time() - s.start) * 1000
            self.out_text += e.get("delta") or ""
        elif etype == "response.function_call_arguments.done":
            self.tool_calls.append(
                {
                    "name": e.get("name"),
                    "call_id": e.get("call_id"),
                    "arguments": e.get("arguments"),
                }
            )
        elif etype == "conversation.item.input_audio_transcription.completed":
            self.input_transcript += e.get("transcript") or ""
        elif etype == "response.done":
            self._finish_turn(e.get("response") or {})
        elif etype == "error":
            err = e.get("error") or {}
            target = self.turn or self._ensure_session()
            target.attributes.setdefault("errors", []).append(
                {"type": err.get("type"), "code": err.get("code"), "message": err.get("message")}
            )


def _tracer(conn: Any) -> RealtimeTracer:
    t = conn.__dict__.get("_tirkit_tracer")
    if t is None:
        t = RealtimeTracer()
        conn.__dict__["_tirkit_tracer"] = t
    return t


def _safe(fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - tracing must never break the voice session
        pass


def wrap_connection_class(cls: Any, *, is_async: bool) -> None:
    """Patch ``send``, ``recv`` and ``close`` of a realtime connection class."""
    from tirkit.instrumentation._patch import patch

    if is_async:

        def make_send(original: Any) -> Any:
            async def send(self: Any, event: Any) -> Any:
                _safe(_tracer(self).on_client_event, event)
                return await original(self, event)

            return send

        def make_recv(original: Any) -> Any:
            async def recv(self: Any) -> Any:
                event = await original(self)
                _safe(_tracer(self).on_server_event, event)
                return event

            return recv

        def make_close(original: Any) -> Any:
            async def close(self: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    return await original(self, *args, **kwargs)
                finally:
                    _safe(_tracer(self).close)

            return close

    else:

        def make_send(original: Any) -> Any:
            def send(self: Any, event: Any) -> Any:
                _safe(_tracer(self).on_client_event, event)
                return original(self, event)

            return send

        def make_recv(original: Any) -> Any:
            def recv(self: Any) -> Any:
                event = original(self)
                _safe(_tracer(self).on_server_event, event)
                return event

            return recv

        def make_close(original: Any) -> Any:
            def close(self: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    return original(self, *args, **kwargs)
                finally:
                    _safe(_tracer(self).close)

            return close

    patch(cls, "send", make_send)
    patch(cls, "recv", make_recv)
    # close() may live on a base class: patch it on this class explicitly.
    if "close" not in cls.__dict__:
        cls.close = cls.close
    patch(cls, "close", make_close)


def instrument() -> bool:
    try:
        from openai.resources.realtime import realtime
    except ImportError:
        return False
    wrap_connection_class(realtime.RealtimeConnection, is_async=False)
    wrap_connection_class(realtime.AsyncRealtimeConnection, is_async=True)
    return True
