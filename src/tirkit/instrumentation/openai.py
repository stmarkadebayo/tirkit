"""Auto-tracing for the OpenAI Python SDK and every OpenAI-compatible server.

Covers chat completions (text, vision input, audio input and output, tools, streaming), the
Responses API (incl. streaming), embeddings, image generation and editing, speech-to-text
(transcriptions and translations, incl. streaming) and text-to-speech (incl. SSE streaming).
Local and small models served through OpenAI-compatible APIs (Ollama, vLLM, LM Studio, llama.cpp)
are traced the same way; ``attributes.provider`` records the host.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlparse

from tirkit.instrumentation._patch import (
    CallSpec,
    file_to_media,
    get,
    patch,
    wav_seconds,
    wrap_method,
)
from tirkit.tracing import Span


def _provider(client: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    base = get(client, "_client", "base_url")
    host = urlparse(str(base)).hostname if base else None
    if host in (None, "api.openai.com"):
        return {"provider": "openai"}
    return {"provider": "openai-compatible", "provider_host": host}


def _dump(obj: Any) -> Any:
    dump = getattr(obj, "model_dump", None)
    return dump(exclude_none=True) if callable(dump) else obj


def _chat_usage(s: Span, usage: Any) -> None:
    if usage is None:
        return
    s.set_usage(
        input_tokens=get(usage, "prompt_tokens"),
        output_tokens=get(usage, "completion_tokens"),
        cached_input_tokens=get(usage, "prompt_tokens_details", "cached_tokens"),
        input_audio_tokens=get(usage, "prompt_tokens_details", "audio_tokens"),
        output_audio_tokens=get(usage, "completion_tokens_details", "audio_tokens"),
    )
    reasoning = get(usage, "completion_tokens_details", "reasoning_tokens")
    if reasoning:
        s.set_usage(reasoning_tokens=reasoning)
        s.attributes["reasoning_tokens"] = reasoning


# -- chat completions -------------------------------------------------------------------------


def _chat_input(kw: dict[str, Any]) -> Any:
    out: dict[str, Any] = {"messages": kw.get("messages")}
    if kw.get("tools"):
        out["tools"] = kw["tools"]
    return out


def _chat_kind(kw: dict[str, Any]) -> str:
    modalities = kw.get("modalities") or []
    return "speech_to_speech" if "audio" in modalities and _has_audio_input(kw) else "llm"


def _has_audio_input(kw: dict[str, Any]) -> bool:
    for m in kw.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") == "input_audio" for p in content
        ):
            return True
    return False


def _chat_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    s.model = get(res, "model") or s.model
    choices = get(res, "choices") or []
    msgs = [_dump(get(c, "message")) for c in choices]
    s.output = msgs[0] if len(msgs) == 1 else msgs
    s.attributes["finish_reason"] = [get(c, "finish_reason") for c in choices]
    s.attributes["response_id"] = get(res, "id")
    _chat_usage(s, get(res, "usage"))


def _chat_stream(s: Span, kw: dict[str, Any]) -> Any:
    choices: dict[int, dict[str, Any]] = {}

    def on_item(chunk: Any) -> None:
        s.model = get(chunk, "model") or s.model
        if get(chunk, "usage") is not None:
            _chat_usage(s, get(chunk, "usage"))
        for c in get(chunk, "choices") or []:
            acc = choices.setdefault(
                get(c, "index") or 0, {"role": "assistant", "content": "", "tool_calls": {}}
            )
            delta = get(c, "delta")
            if get(delta, "content"):
                acc["content"] += get(delta, "content")
            if get(delta, "refusal"):
                acc["refusal"] = acc.get("refusal", "") + get(delta, "refusal")
            for tc in get(delta, "tool_calls") or []:
                slot = acc["tool_calls"].setdefault(
                    get(tc, "index") or 0, {"id": None, "name": None, "arguments": ""}
                )
                slot["id"] = get(tc, "id") or slot["id"]
                slot["name"] = get(tc, "function", "name") or slot["name"]
                slot["arguments"] += get(tc, "function", "arguments") or ""
            if get(c, "finish_reason"):
                acc["finish_reason"] = get(c, "finish_reason")

    def on_done() -> None:
        msgs = []
        for _, acc in sorted(choices.items()):
            acc["tool_calls"] = [v for _, v in sorted(acc["tool_calls"].items())] or None
            msgs.append({k: v for k, v in acc.items() if v not in (None, "")})
        s.output = msgs[0] if len(msgs) == 1 else msgs

    return on_item, on_done


# -- responses API ----------------------------------------------------------------------------


def _responses_input(kw: dict[str, Any]) -> Any:
    out = {"input": kw.get("input")}
    for k in ("instructions", "tools", "previous_response_id"):
        if kw.get(k) is not None and type(kw.get(k)).__name__ not in {"NotGiven", "Omit"}:
            out[k] = kw[k]
    return out


def _responses_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    s.model = get(res, "model") or s.model
    s.output = {
        "output_text": get(res, "output_text"),
        "output": [_dump(o) for o in (get(res, "output") or [])],
    }
    s.attributes["response_id"] = get(res, "id")
    s.attributes["status"] = get(res, "status")
    usage = get(res, "usage")
    if usage is not None:
        s.set_usage(
            input_tokens=get(usage, "input_tokens"),
            output_tokens=get(usage, "output_tokens"),
            cached_input_tokens=get(usage, "input_tokens_details", "cached_tokens"),
        )
        reasoning = get(usage, "output_tokens_details", "reasoning_tokens")
        if reasoning:
            s.set_usage(reasoning_tokens=reasoning)
            s.attributes["reasoning_tokens"] = reasoning


def _responses_stream(s: Span, kw: dict[str, Any]) -> Any:
    state: dict[str, Any] = {"final": None, "text": ""}

    def on_item(event: Any) -> None:
        etype = get(event, "type") or ""
        if etype == "response.output_text.delta":
            state["text"] += get(event, "delta") or ""
        elif etype in ("response.completed", "response.incomplete", "response.failed"):
            state["final"] = get(event, "response")

    def on_done() -> None:
        if state["final"] is not None:
            _responses_result(s, state["final"], kw)
        else:
            s.output = {"output_text": state["text"]}

    return on_item, on_done


# -- embeddings -------------------------------------------------------------------------------


def _embedding_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    s.model = get(res, "model") or s.model
    data = get(res, "data") or []
    first = get(data[0], "embedding") if data else None
    s.output = {"count": len(data), "dimensions": len(first) if isinstance(first, list) else None}
    usage = get(res, "usage")
    s.set_usage(input_tokens=get(usage, "prompt_tokens"))


# -- images -----------------------------------------------------------------------------------


def _images_input(kw: dict[str, Any]) -> Any:
    out: dict[str, Any] = {"prompt": kw.get("prompt")}
    for key in ("image", "mask"):
        val = kw.get(key)
        if val is not None and type(val).__name__ not in {"NotGiven", "Omit"}:
            out[key] = (
                [file_to_media(v) for v in val] if isinstance(val, list) else file_to_media(val)
            )
    return out


def _images_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    data = get(res, "data") or []
    s.output = [_dump(d) for d in data]  # b64_json is moved to the media store by the exporter
    usage = get(res, "usage")
    s.set_usage(
        output_images=len(data) or None,
        input_tokens=get(usage, "input_tokens"),
        output_tokens=get(usage, "output_tokens"),
    )


# -- speech to text ---------------------------------------------------------------------------


def _stt_input(kw: dict[str, Any]) -> Any:
    media = file_to_media(kw.get("file"))
    out: dict[str, Any] = {"audio": media}
    for k in ("language", "prompt"):
        if isinstance(kw.get(k), str):
            out[k] = kw[k]
    return out


def _audio_seconds_from_input(s: Span, kw: dict[str, Any]) -> None:
    media = file_to_media(kw.get("file"))
    seconds = wav_seconds(getattr(media, "data", None))
    if seconds:
        s.attributes["audio_seconds"] = seconds


def _stt_usage(s: Span, usage: Any) -> None:
    if usage is None:
        return
    if get(usage, "type") == "duration" or get(usage, "seconds") is not None:
        s.set_usage(input_audio_seconds=get(usage, "seconds"))
        s.attributes["audio_seconds"] = get(usage, "seconds")
    else:
        s.set_usage(
            input_tokens=get(usage, "input_tokens"),
            output_tokens=get(usage, "output_tokens"),
            input_audio_tokens=get(usage, "input_token_details", "audio_tokens"),
        )


def _rtf(s: Span) -> None:
    seconds = s.attributes.get("audio_seconds")
    if seconds and s.end and seconds > 0:
        s.attributes["real_time_factor"] = (s.end - s.start) / seconds


def _stt_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    _audio_seconds_from_input(s, kw)
    if isinstance(res, str):
        s.output = {"text": res}
    else:
        s.output = {"text": get(res, "text")}
        segments = get(res, "segments")
        if segments:
            s.output["segments"] = len(segments)
        lang = get(res, "language")
        if lang:
            s.attributes["language"] = lang
        _stt_usage(s, get(res, "usage"))
        if get(res, "duration") and "audio_seconds" not in s.attributes:
            s.attributes["audio_seconds"] = float(get(res, "duration"))
    if "input_audio_seconds" not in s.usage and s.attributes.get("audio_seconds"):
        s.set_usage(input_audio_seconds=s.attributes["audio_seconds"])
    import time as _time

    s.end = _time.time()
    _rtf(s)


def _stt_stream(s: Span, kw: dict[str, Any]) -> Any:
    state = {"text": ""}

    def on_item(event: Any) -> None:
        etype = get(event, "type") or ""
        if etype.endswith(".delta"):
            state["text"] += get(event, "delta") or ""
        elif etype.endswith(".done"):
            state["text"] = get(event, "text") or state["text"]
            _stt_usage(s, get(event, "usage"))

    def on_done() -> None:
        _audio_seconds_from_input(s, kw)
        s.output = {"text": state["text"]}
        import time as _time

        s.end = _time.time()
        _rtf(s)

    return on_item, on_done


# -- text to speech ---------------------------------------------------------------------------

_TTS_MIME = {
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}


def _tts_input(kw: dict[str, Any]) -> Any:
    out = {"text": kw.get("input"), "voice": kw.get("voice")}
    if isinstance(kw.get("instructions"), str):
        out["instructions"] = kw["instructions"]
    return out


def _tts_chars(s: Span, kw: dict[str, Any]) -> None:
    if isinstance(kw.get("input"), str):
        s.set_usage(input_characters=len(kw["input"]))


def _tts_audio(s: Span, audio: bytes, kw: dict[str, Any]) -> None:
    from tirkit.media import Attachment

    raw_fmt = kw.get("response_format")
    fmt: str = raw_fmt if isinstance(raw_fmt, str) else "mp3"
    s.output = {"audio": Attachment(data=audio, mime=_TTS_MIME.get(fmt))}
    seconds = wav_seconds(audio)
    if seconds:
        s.set_usage(output_audio_seconds=seconds)
        s.attributes["audio_seconds"] = seconds


def _tts_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    _tts_chars(s, kw)
    content = getattr(res, "content", None)
    if isinstance(content, bytes):
        _tts_audio(s, content, kw)


def _tts_is_stream(kw: dict[str, Any], res: Any) -> bool:
    return kw.get("stream_format") == "sse"


def _tts_stream(s: Span, kw: dict[str, Any]) -> Any:
    chunks: list[bytes] = []

    def on_item(event: Any) -> None:
        etype = get(event, "type") or ""
        if etype.endswith("audio.delta") and get(event, "audio"):
            chunks.append(base64.b64decode(get(event, "audio")))
        elif etype.endswith("audio.done"):
            usage = get(event, "usage")
            s.set_usage(
                input_tokens=get(usage, "input_tokens"), output_tokens=get(usage, "output_tokens")
            )

    def on_done() -> None:
        _tts_chars(s, kw)
        if chunks:
            _tts_audio(s, b"".join(chunks), kw)

    return on_item, on_done


# -- registration -----------------------------------------------------------------------------


def _llm(kw: dict[str, Any]) -> str:
    return "llm"


def instrument() -> bool:
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    from openai.resources import embeddings, images
    from openai.resources.audio import speech, transcriptions, translations
    from openai.resources.chat.completions import completions as chat
    from openai.resources.responses import responses

    chat_spec = CallSpec(
        name="openai.chat",
        kind=_chat_kind,
        build_input=_chat_input,
        param_exclude={"messages", "model", "tools"},
        on_result=_chat_result,
        stream_handlers=_chat_stream,
        attributes=_provider,
    )
    responses_spec = CallSpec(
        name="openai.responses",
        kind=_llm,
        build_input=_responses_input,
        param_exclude={"input", "model", "instructions", "tools"},
        on_result=_responses_result,
        stream_handlers=_responses_stream,
        attributes=_provider,
    )
    embed_spec = CallSpec(
        name="openai.embeddings",
        kind=lambda kw: "embedding",
        build_input=lambda kw: {"input": kw.get("input")},
        param_exclude={"input", "model"},
        on_result=_embedding_result,
        attributes=_provider,
    )
    image_spec = CallSpec(
        name="openai.images",
        kind=lambda kw: "image_generation",
        build_input=_images_input,
        param_exclude={"prompt", "model", "image", "mask"},
        on_result=_images_result,
        attributes=_provider,
    )
    stt_spec = CallSpec(
        name="openai.transcription",
        kind=lambda kw: "speech_to_text",
        build_input=_stt_input,
        param_exclude={"file", "model", "prompt", "language"},
        on_result=_stt_result,
        stream_handlers=_stt_stream,
        attributes=_provider,
    )
    translate_spec = CallSpec(
        name="openai.translation",
        kind=lambda kw: "speech_to_text",
        build_input=_stt_input,
        param_exclude={"file", "model", "prompt"},
        on_result=_stt_result,
        attributes=lambda c, kw: {**_provider(c, kw), "task": "translate"},
    )
    tts_spec = CallSpec(
        name="openai.speech",
        kind=lambda kw: "text_to_speech",
        build_input=_tts_input,
        param_exclude={"input", "model", "voice", "instructions"},
        on_result=_tts_result,
        is_stream=_tts_is_stream,
        stream_handlers=_tts_stream,
        attributes=_provider,
    )
    targets = [
        (chat.Completions, "create", chat_spec),
        (chat.AsyncCompletions, "create", chat_spec),
        (responses.Responses, "create", responses_spec),
        (responses.AsyncResponses, "create", responses_spec),
        (embeddings.Embeddings, "create", embed_spec),
        (embeddings.AsyncEmbeddings, "create", embed_spec),
        (images.Images, "generate", image_spec),
        (images.AsyncImages, "generate", image_spec),
        (images.Images, "edit", image_spec),
        (images.AsyncImages, "edit", image_spec),
        (transcriptions.Transcriptions, "create", stt_spec),
        (transcriptions.AsyncTranscriptions, "create", stt_spec),
        (translations.Translations, "create", translate_spec),
        (translations.AsyncTranslations, "create", translate_spec),
        (speech.Speech, "create", tts_spec),
        (speech.AsyncSpeech, "create", tts_spec),
    ]
    for owner, attr, spec in targets:
        patch(owner, attr, wrap_method(spec))
    from tirkit.instrumentation import realtime as realtime_voice

    realtime_voice.instrument()
    return True
