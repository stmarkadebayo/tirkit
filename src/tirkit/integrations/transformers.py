"""Hugging Face ``transformers`` pipelines: small local models, speech and vision, in-process.

    import tirkit
    from tirkit.integrations.transformers import instrument
    instrument()   # every Pipeline.__call__ becomes a span, kind chosen from the task

Task → span kind: text-generation / text2text-generation / summarization / translation /
image-text-to-text / image-to-text / visual-question-answering → ``llm``;
automatic-speech-recognition → ``speech_to_text``; text-to-speech / text-to-audio →
``text_to_speech``; feature-extraction / sentence-similarity → ``embedding``;
text-to-image → ``image_generation``. Audio and images in inputs and outputs become media.
"""

from __future__ import annotations

import functools
import io
import time
import wave
from typing import Any

from tirkit.media import Attachment
from tirkit.tracing import finish_span, start_span

TASK_KINDS = {
    "text-generation": "llm",
    "text2text-generation": "llm",
    "summarization": "llm",
    "translation": "llm",
    "image-text-to-text": "llm",
    "image-to-text": "llm",
    "visual-question-answering": "llm",
    "automatic-speech-recognition": "speech_to_text",
    "text-to-speech": "text_to_speech",
    "text-to-audio": "text_to_speech",
    "feature-extraction": "embedding",
    "sentence-similarity": "embedding",
    "text-to-image": "image_generation",
}


def _wav_from_array(audio: Any, rate: int) -> bytes | None:
    try:
        import numpy as np

        arr = np.asarray(audio, dtype="float32").reshape(-1)
        pcm = (np.clip(arr, -1, 1) * 32767).astype("<i2").tobytes()
    except Exception:  # noqa: BLE001
        return None
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm)
    return buf.getvalue()


def _media(value: Any) -> Any:
    """PIL images, raw bytes, audio arrays and file paths to media; everything else unchanged."""
    if isinstance(value, bytes | bytearray):
        return Attachment(data=bytes(value))
    save = getattr(value, "save", None)
    if callable(save) and hasattr(value, "size") and hasattr(value, "mode"):  # PIL image
        buf = io.BytesIO()
        value.save(buf, format="PNG")
        return Attachment(data=buf.getvalue(), mime="image/png")
    if (
        isinstance(value, dict)
        and "audio" in value
        and ("sampling_rate" in value or "sample_rate" in value)
    ):
        rate = value.get("sampling_rate") or value.get("sample_rate") or 16000
        wav = _wav_from_array(value["audio"], int(rate))
        rest = {k: v for k, v in value.items() if k != "audio"}
        return {**rest, "audio": Attachment(data=wav, mime="audio/wav") if wav else "<audio>"}
    if isinstance(value, dict) and "raw" in value and "sampling_rate" in value:
        wav = _wav_from_array(value["raw"], int(value["sampling_rate"]))
        return {"audio": Attachment(data=wav, mime="audio/wav") if wav else "<audio>"}
    if isinstance(value, list):
        return [_media(v) for v in value]
    return value


def _model_name(pipe: Any) -> str | None:
    model = getattr(pipe, "model", None)
    cfg = getattr(model, "config", None)
    return getattr(cfg, "_name_or_path", None) or getattr(model, "name_or_path", None)


def _wrap(original: Any) -> Any:
    @functools.wraps(original)
    def call(self: Any, inputs: Any = None, *args: Any, **kwargs: Any) -> Any:
        task = getattr(self, "task", "") or ""
        kind = TASK_KINDS.get(task, "generic")
        s = start_span(
            f"hf:{task or type(self).__name__}",
            kind=kind,
            input=_media(inputs),
            model=_model_name(self),
            model_params=dict(kwargs),
            task=task,
            provider="transformers",
        )
        t0 = time.perf_counter()
        try:
            out = original(self, inputs, *args, **kwargs)
        except BaseException as exc:
            s.record_error(exc)
            finish_span(s)
            raise
        s.output = _media(out)
        if kind == "speech_to_text" and isinstance(inputs, dict):
            arr = inputs.get("raw", inputs.get("array"))
            rate = inputs.get("sampling_rate")
            if arr is not None and rate:
                seconds = len(arr) / float(rate)
                s.set_usage(input_audio_seconds=seconds)
                s.attributes["real_time_factor"] = (time.perf_counter() - t0) / seconds
        if kind == "text_to_speech" and isinstance(out, dict) and "audio" in out:
            rate = out.get("sampling_rate")
            try:
                n = len(getattr(out["audio"], "reshape", lambda *_: out["audio"])(-1))
                if rate:
                    s.set_usage(output_audio_seconds=n / float(rate))
            except TypeError:
                pass
            if isinstance(inputs, str):
                s.set_usage(input_characters=len(inputs))
        finish_span(s)
        return out

    call.__tirkit_wrapped__ = True  # type: ignore[attr-defined]
    return call


def instrument() -> bool:
    """Patch ``transformers.Pipeline.__call__``. Returns False when transformers is missing."""
    try:
        from transformers.pipelines.base import Pipeline
    except ImportError:
        return False
    if not getattr(Pipeline.__call__, "__tirkit_wrapped__", False):
        Pipeline.__call__ = _wrap(Pipeline.__call__)  # type: ignore[method-assign]
    return True
