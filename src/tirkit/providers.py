"""Model calls made by the server itself: playground, UI-started experiments and online evals.

Configured with environment variables on the server:

- ``LLM_BASE_URL`` / ``LLM_API_KEY``: any OpenAI-compatible endpoint (OpenAI, OpenRouter, a local
  LM Studio or Ollama, an Eliakim gateway). Model names are passed through.
- ``ANTHROPIC_API_KEY`` (or an ``ant auth login`` profile): models named ``anthropic/<model>``
  go to the Anthropic Messages API through the official SDK.

Every call is recorded as an ``llm`` span (tokens, cost, latency) in the current sink.
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from tirkit import pricing
from tirkit.tracing import span

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class ProviderNotConfigured(RuntimeError):
    pass


@dataclass
class ModelResult:
    text: str
    model: str
    usage: dict[str, float] = field(default_factory=dict)
    cost_usd: float | None = None
    latency_ms: float = 0.0


def providers_status() -> dict[str, Any]:
    base = os.environ.get("LLM_BASE_URL")
    return {
        "openai_compatible": {"configured": bool(base), "base_url": base},
        "anthropic": {"configured": bool(os.environ.get("ANTHROPIC_API_KEY"))},
    }


def list_models(timeout: float = 5.0) -> list[str]:
    """Models the OpenAI-compatible endpoint reports, plus a note for Anthropic."""
    import httpx

    out: list[str] = []
    base = os.environ.get("LLM_BASE_URL")
    if base:
        try:
            r = httpx.get(f"{base.rstrip('/')}/models", headers=_auth(), timeout=timeout)
            r.raise_for_status()
            out += [m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and "id" in m]
        except Exception:  # noqa: BLE001 - listing is best effort
            pass
    if os.environ.get("ANTHROPIC_API_KEY"):
        out += [
            "anthropic/claude-opus-5-5",
            "anthropic/claude-sonnet-5-5",
            "anthropic/claude-haiku-5-5",
        ]
    return out


def _auth() -> dict[str, str]:
    key = os.environ.get("LLM_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _to_openai_content(content: Any) -> Any:
    return content


def call_model(
    model: str,
    messages: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
    *,
    span_name: str = "model-call",
    kind: str = "llm",
    timeout: float = 300.0,
    strip_thinking: bool = True,
) -> ModelResult:
    """Call a chat model and trace it. ``messages`` use the OpenAI shape (role, content)."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    with span(span_name, kind=kind, model=model, input={"messages": messages}) as s:
        s.model_params = dict(params)
        t0 = time.perf_counter()
        if model.startswith("anthropic/"):
            text, usage, served = _anthropic(model.split("/", 1)[1], messages, params)
        else:
            text, usage, served = _openai_compatible(model, messages, params, timeout)
        if strip_thinking:
            text = _THINK.sub("", text).strip()
        s.output = text
        s.model = served
        s.set_usage(**usage)
        s.cost_usd = pricing.cost(served, s.usage)
        return ModelResult(
            text, served, dict(s.usage), s.cost_usd, (time.perf_counter() - t0) * 1000
        )


def _openai_compatible(
    model: str, messages: list[dict[str, Any]], params: dict[str, Any], timeout: float
) -> tuple[str, dict[str, float], str]:
    import httpx

    base = os.environ.get("LLM_BASE_URL")
    if not base:
        raise ProviderNotConfigured("set LLM_BASE_URL (and LLM_API_KEY) on the server")
    body = {"model": model, "messages": messages, **params}
    r = httpx.post(
        f"{base.rstrip('/')}/chat/completions", json=body, headers=_auth(), timeout=timeout
    )
    if r.status_code >= 400:
        raise RuntimeError(f"model endpoint returned {r.status_code}: {r.text[:300]}")
    data = r.json()
    msg = data["choices"][0]["message"]
    text = msg.get("content") or ""
    u = data.get("usage") or {}
    usage = {
        "input_tokens": u.get("prompt_tokens"),
        "output_tokens": u.get("completion_tokens"),
        "cached_input_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
    }
    return str(text), {k: float(v) for k, v in usage.items() if v}, str(data.get("model") or model)


def _anthropic(
    model: str, messages: list[dict[str, Any]], params: dict[str, Any]
) -> tuple[str, dict[str, float], str]:
    try:
        import anthropic
    except ImportError as exc:
        raise ProviderNotConfigured("pip install anthropic to use anthropic/ models") from exc
    system = "\n\n".join(str(m["content"]) for m in messages if m.get("role") == "system")
    turns = []
    for m in messages:
        if m.get("role") == "system":
            continue
        content = m.get("content")
        if isinstance(content, list):  # OpenAI parts -> Anthropic blocks (text and data-URI images)
            blocks = []
            for p in content:
                if p.get("type") == "text":
                    blocks.append({"type": "text", "text": p["text"]})
                elif p.get("type") == "image_url":
                    url = p["image_url"]["url"]
                    if url.startswith("data:"):
                        mime, b64 = url[5:].split(";base64,", 1)
                        blocks.append(
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": mime, "data": b64},
                            }
                        )
                    else:
                        blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            content = blocks
        turns.append({"role": m["role"], "content": content})
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": int(params.pop("max_tokens", 4096)),
        "messages": turns,
    }
    if system:
        kwargs["system"] = system
    msg = anthropic.Anthropic().messages.create(**kwargs)
    if msg.stop_reason == "refusal":
        raise RuntimeError("model refused")
    u = msg.usage
    usage = {
        "input_tokens": (u.input_tokens or 0)
        + (u.cache_read_input_tokens or 0)
        + (u.cache_creation_input_tokens or 0),
        "cached_input_tokens": u.cache_read_input_tokens or 0,
        "cache_write_tokens": u.cache_creation_input_tokens or 0,
        "output_tokens": u.output_tokens,
    }
    text = "".join(b.text for b in msg.content if b.type == "text")
    return text, {k: float(v) for k, v in usage.items() if v}, model


class ServerJudgeModel:
    """Adapter so :class:`tirkit.judge.LLMJudge` can use a server-configured model."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.last_cost: float | None = None

    def __call__(self, prompt: str, images: list[tuple[bytes, str]] | None = None) -> str:
        content: Any = prompt
        if images:
            content = [{"type": "text", "text": prompt}] + [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{m};base64,{base64.b64encode(d).decode()}"},
                }
                for d, m in images
            ]
        res = call_model(
            self.model,
            [{"role": "user", "content": content}],
            {"temperature": 0} if not self.model.startswith("anthropic/") else {},
            span_name="judge",
            kind="evaluator",
        )
        self.last_cost = res.cost_usd
        return res.text
