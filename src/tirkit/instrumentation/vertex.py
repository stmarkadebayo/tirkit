"""Auto-tracing for the Vertex AI SDK (``vertexai.generative_models.GenerativeModel``):
``generate_content`` and ``generate_content_async``, with and without ``stream=True``.

Google now recommends the Gen AI SDK (``google-genai``) for Vertex; it is traced by the
``gemini`` instrumentation (``vertexai=True`` clients are recorded with provider ``vertex_ai``).
This adapter is for code still on the older ``vertexai`` package.
"""

from __future__ import annotations

from typing import Any

from tirkit.instrumentation._patch import CallSpec, get, patch, wrap_method
from tirkit.instrumentation.gemini import _usage
from tirkit.serialize import to_jsonable
from tirkit.tracing import Span

_PARAM_KEYS = {
    "temperature",
    "top_p",
    "top_k",
    "max_output_tokens",
    "candidate_count",
    "stop_sequences",
    "seed",
    "response_mime_type",
    "presence_penalty",
    "frequency_penalty",
}


def _model_name(model: Any, kw: dict[str, Any]) -> str | None:
    name = get(model, "_model_name") or get(model, "model_name")
    return str(name).rsplit("/", 1)[-1] if name else None


def _config(kw: dict[str, Any]) -> dict[str, Any]:
    cfg = kw.get("generation_config")
    cfg = to_jsonable(cfg) if cfg is not None and not isinstance(cfg, dict) else cfg
    return {k: v for k, v in (cfg or {}).items() if k in _PARAM_KEYS and v is not None}


def _input(kw: dict[str, Any]) -> Any:
    out: dict[str, Any] = {"contents": to_jsonable(kw.get("contents"))}
    if kw.get("tools"):
        out["tools"] = to_jsonable(kw["tools"])
    return out


def _output(res: Any) -> Any:
    candidates = get(res, "candidates") or []
    contents = [to_jsonable(get(c, "content")) for c in candidates]
    return contents[0] if len(contents) == 1 else contents


def _result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    s.output = _output(res)
    s.attributes["finish_reason"] = [
        str(get(c, "finish_reason")) for c in get(res, "candidates") or []
    ]
    _usage(s, get(res, "usage_metadata"))


def _stream(s: Span, kw: dict[str, Any]) -> Any:
    state: dict[str, Any] = {"parts": [], "usage": None}

    def on_item(chunk: Any) -> None:
        for c in get(chunk, "candidates") or []:
            for part in get(c, "content", "parts") or []:
                state["parts"].append(to_jsonable(part))
        if get(chunk, "usage_metadata") is not None:
            state["usage"] = get(chunk, "usage_metadata")

    def on_done() -> None:
        s.output = {"role": "model", "parts": state["parts"]}
        _usage(s, state["usage"])

    return on_item, on_done


def instrument() -> bool:
    try:
        from vertexai.generative_models import GenerativeModel
    except ImportError:
        return False
    base: dict[str, Any] = {
        "kind": lambda kw: "llm",
        "build_input": _input,
        "build_params": _config,
        "model": _model_name,
        "arg_names": ("contents",),
        "on_result": _result,
        "stream_handlers": _stream,
        "attributes": lambda client, kw: {"provider": "vertex_ai"},
    }
    spec = CallSpec(name="vertex.generate_content", **base)
    for method in ("generate_content", "generate_content_async"):
        patch(GenerativeModel, method, wrap_method(spec))
    return True
