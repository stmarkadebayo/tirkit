"""Auto-tracing for Google's Gen AI SDK (``pip install google-genai``), Gemini API and Vertex AI.

Covers ``models.generate_content`` and ``generate_content_stream`` (text, images, audio and video
input; text, audio and image output; thinking tokens; tools), ``embed_content`` and
``generate_images``, sync and async (``client.aio``). Inline image and audio parts become media;
raw PCM audio returned by speech models is wrapped as WAV so it plays in the UI.
"""

from __future__ import annotations

import base64
import io
import re
import wave
from typing import Any

from tirkit.instrumentation._patch import CallSpec, get, patch, wrap_method
from tirkit.media import Attachment
from tirkit.serialize import to_jsonable
from tirkit.tracing import Span

_PARAMS = {
    "temperature",
    "top_p",
    "top_k",
    "max_output_tokens",
    "candidate_count",
    "stop_sequences",
    "seed",
    "response_mime_type",
    "response_modalities",
    "thinking_config",
    "presence_penalty",
    "frequency_penalty",
}


def _pcm_to_wav(data: bytes, mime: str) -> tuple[bytes, str]:
    rate = int(m.group(1)) if (m := re.search(r"rate=(\d+)", mime)) else 24000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    return buf.getvalue(), "audio/wav"


def _inline_to_media(node: Any) -> Any:
    """Turn ``{"inline_data": {"data": bytes|b64, "mime_type": ...}}`` parts into media."""
    if isinstance(node, list):
        return [_inline_to_media(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: _inline_to_media(v) for k, v in node.items() if v is not None}
    blob = node.get("inline_data")
    if isinstance(blob, dict) and blob.get("data") is not None:
        data = blob["data"]
        if isinstance(data, str):
            try:
                data = base64.b64decode(data)
            except ValueError:
                return out
        mime = str(blob.get("mime_type") or "application/octet-stream")
        if mime.startswith(("audio/L16", "audio/pcm")):
            data, mime = _pcm_to_wav(bytes(data), mime)
        out["inline_data"] = Attachment(data=bytes(data), mime=mime)
    return out


def _config(kw: dict[str, Any]) -> dict[str, Any]:
    cfg = to_jsonable(kw.get("config")) or {}
    return cfg if isinstance(cfg, dict) else {}


def _kind(kw: dict[str, Any]) -> str:
    modalities = [str(m).upper() for m in (_config(kw).get("response_modalities") or [])]
    if "AUDIO" in modalities:
        return (
            "text_to_speech"
            if "audio" not in str(kw.get("contents")).lower()
            else "speech_to_speech"
        )
    if "IMAGE" in modalities:
        return "image_generation"
    return "llm"


def _input(kw: dict[str, Any]) -> Any:
    cfg = _config(kw)
    out: dict[str, Any] = {"contents": _inline_to_media(to_jsonable(kw.get("contents")))}
    if cfg.get("system_instruction") is not None:
        out["system"] = cfg["system_instruction"]
    if cfg.get("tools"):
        out["tools"] = cfg["tools"]
    return out


def _params(kw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in _config(kw).items() if k in _PARAMS and v is not None}


def _modality_tokens(details: Any, modality: str) -> float | None:
    for d in details or []:
        if str(get(d, "modality")).upper().endswith(modality):
            return get(d, "token_count")
    return None


def _usage(s: Span, u: Any) -> None:
    if u is None:
        return
    out = (get(u, "candidates_token_count") or 0) + (get(u, "thoughts_token_count") or 0)
    s.set_usage(
        input_tokens=get(u, "prompt_token_count"),
        output_tokens=out or None,
        cached_input_tokens=get(u, "cached_content_token_count"),
        input_audio_tokens=_modality_tokens(get(u, "prompt_tokens_details"), "AUDIO"),
        output_audio_tokens=_modality_tokens(get(u, "candidates_tokens_details"), "AUDIO"),
    )
    if get(u, "thoughts_token_count"):
        s.set_usage(reasoning_tokens=get(u, "thoughts_token_count"))
        s.attributes["reasoning_tokens"] = get(u, "thoughts_token_count")


def _result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    s.model = get(res, "model_version") or s.model
    candidates = get(res, "candidates") or []
    contents = [_inline_to_media(to_jsonable(get(c, "content"))) for c in candidates]
    s.output = contents[0] if len(contents) == 1 else contents
    s.attributes["finish_reason"] = [str(get(c, "finish_reason")) for c in candidates]
    _usage(s, get(res, "usage_metadata"))


def _stream(s: Span, kw: dict[str, Any]) -> Any:
    state: dict[str, Any] = {"text": "", "parts": [], "usage": None}

    def on_item(chunk: Any) -> None:
        s.model = get(chunk, "model_version") or s.model
        for c in get(chunk, "candidates") or []:
            for p in get(c, "content", "parts") or []:
                if get(p, "text"):
                    state["text"] += get(p, "text")
                elif get(p, "inline_data") is not None or get(p, "function_call") is not None:
                    state["parts"].append(_inline_to_media(to_jsonable(p)))
        if get(chunk, "usage_metadata") is not None:
            state["usage"] = get(chunk, "usage_metadata")

    def on_done() -> None:
        parts = ([{"text": state["text"]}] if state["text"] else []) + state["parts"]
        s.output = {"role": "model", "parts": parts}
        _usage(s, state["usage"])

    return on_item, on_done


def _embed_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    emb = get(res, "embeddings") or []
    first = get(emb[0], "values") if emb else None
    s.output = {"count": len(emb), "dimensions": len(first) if isinstance(first, list) else None}


def _images_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    images = get(res, "generated_images") or []
    out = []
    for g in images:
        img = get(g, "image")
        data = get(img, "image_bytes")
        if isinstance(data, bytes):
            out.append(Attachment(data=data, mime=get(img, "mime_type") or None))
    s.output = out
    s.set_usage(output_images=len(images) or None)


def instrument() -> bool:
    try:
        from google.genai import models
    except ImportError:
        return False

    def attrs(client: Any, kw: dict[str, Any]) -> dict[str, Any]:
        vertex = bool(get(client, "_api_client", "vertexai"))
        return {"provider": "vertex_ai" if vertex else "gemini"}

    gen = CallSpec(
        build_params=_params,
        name="gemini.generate_content",
        kind=_kind,
        build_input=_input,
        on_result=_result,
        attributes=attrs,
    )
    stream = CallSpec(
        build_params=_params,
        name="gemini.generate_content",
        kind=_kind,
        build_input=_input,
        on_result=_result,
        is_stream=lambda kw, res: True,
        stream_handlers=_stream,
        attributes=lambda c, kw: {**attrs(c, kw), "streaming": True},
    )
    embed = CallSpec(
        build_params=_params,
        name="gemini.embed_content",
        kind=lambda kw: "embedding",
        build_input=lambda kw: {"contents": to_jsonable(kw.get("contents"))},
        on_result=_embed_result,
        attributes=attrs,
    )
    images = CallSpec(
        build_params=_params,
        name="gemini.generate_images",
        kind=lambda kw: "image_generation",
        build_input=lambda kw: {"prompt": kw.get("prompt")},
        on_result=_images_result,
        attributes=attrs,
    )
    for owner in (models.Models, models.AsyncModels):
        for attr, spec in (
            ("generate_content", gen),
            ("generate_content_stream", stream),
            ("embed_content", embed),
            ("generate_images", images),
        ):
            patch(owner, attr, wrap_method(spec))
    return True
