"""Auto-tracing for LiteLLM: ``litellm.completion`` / ``acompletion`` (incl. streaming) and
``litellm.embedding`` / ``aembedding``. Responses are OpenAI-shaped.

One LiteLLM call is one span. LiteLLM calls provider SDKs underneath; spans for those calls
are not recorded while a LiteLLM call is running, so cost is not counted twice. Call
``tirkit.instrument()`` before ``from litellm import completion`` so the name you import is the
traced one (``litellm.completion(...)`` always works).
"""

from __future__ import annotations

from typing import Any

from tirkit.instrumentation._patch import CallSpec, get, patch, wrap_method
from tirkit.instrumentation.openai import _chat_result, _chat_stream
from tirkit.tracing import Span

_EXCLUDE = {
    "model",
    "messages",
    "tools",
    "api_key",
    "api_base",
    "base_url",
    "api_version",
    "extra_headers",
    "mock_response",
    "metadata",
    "timeout",
    "stream",
    "stream_options",
    "functions",
}


def _provider(client: Any, kw: dict[str, Any]) -> dict[str, Any]:
    model = kw.get("model")
    if isinstance(model, str) and "/" in model:
        return {"provider": model.split("/", 1)[0], "via": "litellm"}
    return {"provider": kw.get("custom_llm_provider") or "unknown", "via": "litellm"}


def _input(kw: dict[str, Any]) -> Any:
    out: dict[str, Any] = {"messages": kw.get("messages")}
    if kw.get("tools"):
        out["tools"] = kw["tools"]
    return out


def _result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    _chat_result(s, res, kw)
    # LiteLLM prices the call itself; keep its figure when ours cannot price the model.
    hidden = get(res, "_hidden_params") or {}
    if isinstance(hidden, dict) and isinstance(hidden.get("response_cost"), int | float):
        s.attributes["litellm_cost_usd"] = float(hidden["response_cost"])


def _embed_result(s: Span, res: Any, kw: dict[str, Any]) -> None:
    s.model = get(res, "model") or s.model
    s.output = {"embeddings": len(get(res, "data") or [])}
    s.set_usage(input_tokens=get(res, "usage", "prompt_tokens"))


def instrument() -> bool:
    try:
        import litellm
    except ImportError:
        return False
    chat = CallSpec(
        name="litellm.completion",
        kind=lambda kw: "llm",
        build_input=_input,
        param_exclude=_EXCLUDE,
        on_result=_result,
        stream_handlers=_chat_stream,
        attributes=_provider,
        function=True,
        suppress_nested=True,
    )
    embed = CallSpec(
        name="litellm.embedding",
        kind=lambda kw: "embedding",
        build_input=lambda kw: {"input": kw.get("input")},
        param_exclude=_EXCLUDE | {"input"},
        on_result=_embed_result,
        attributes=_provider,
        function=True,
        suppress_nested=True,
    )
    ok = False
    for name, spec in (
        ("completion", chat),
        ("acompletion", chat),
        ("embedding", embed),
        ("aembedding", embed),
    ):
        ok = patch(litellm, name, wrap_method(spec)) or ok
    return ok
