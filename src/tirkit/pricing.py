"""Model prices and cost calculation for every metered unit (SPEC FR-4, FR-31).

Usage units (all optional, non-negative):

- ``input_tokens``: all input tokens, every modality, cached or not.
- ``cached_input_tokens``, ``cache_write_tokens``, ``input_audio_tokens``: subsets of input tokens.
- ``output_tokens``: all output tokens; ``output_audio_tokens`` is a subset.
- ``input_audio_seconds``, ``output_audio_seconds``: speech-to-text input, text-to-speech output.
- ``input_characters``, ``output_characters``: e.g. text-to-speech input.
- ``input_images``, ``output_images``: e.g. image generation.
- ``requests``: flat per-call prices (rerank, search).

Default prices come from ``tirkit/data/prices.json`` (LiteLLM's price map, MIT licence); refresh it
with ``scripts/update_prices.py``. Override or add models with :func:`register_price`.
"""

from __future__ import annotations

import json
import threading
from functools import lru_cache
from importlib import resources
from typing import Any

UNITS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "input_audio_tokens",
    "output_audio_tokens",
    "input_audio_seconds",
    "output_audio_seconds",
    "input_characters",
    "output_characters",
    "input_images",
    "output_images",
    "requests",
)

_overrides: dict[str, dict[str, float]] = {}
_lock = threading.Lock()


@lru_cache(maxsize=1)
def _builtin() -> dict[str, dict[str, float]]:
    raw = json.loads(resources.files("tirkit.data").joinpath("prices.json").read_text())
    return {name: entry["prices"] for name, entry in raw["models"].items()}


def register_price(model: str, **prices: float) -> None:
    """Set USD-per-unit prices for a model.

    Example: ``register_price("my-tts", input_characters=1e-5)``.
    """
    unknown = set(prices) - set(UNITS)
    if unknown:
        raise ValueError(f"unknown price units: {sorted(unknown)}")
    with _lock:
        _overrides[model] = dict(prices)
    lookup_price.cache_clear()


@lru_cache(maxsize=4096)
def lookup_price(model: str) -> dict[str, float] | None:
    """Find prices for a model name: exact, then without a ``provider/`` prefix, then the longest
    known name that the model starts with (``gpt-4o-2024-08-06`` -> ``gpt-4o``)."""
    tables = (_overrides, _builtin())
    candidates = [model]
    if "/" in model:
        candidates.append(model.split("/", 1)[1])
    for table in tables:
        for name in candidates:
            if name in table:
                return table[name]
    for table in tables:
        for name in candidates:
            best = ""
            for known in table:
                if name.startswith(known) and len(known) > len(best) and "/" not in known:
                    best = known
            if best and len(best) >= 4:
                return table[best]
    return None


def cost(
    model: str | None, usage: dict[str, Any], overrides: dict[str, dict[str, float]] | None = None
) -> float | None:
    """USD cost of one call, or None when the model or its prices are unknown.

    ``overrides`` (model -> prices) take precedence, e.g. a project's own price table.
    """
    if not model or not usage:
        return None
    p = (overrides or {}).get(model) or lookup_price(model)
    if p is None:
        return None
    u = {k: float(v) for k, v in usage.items() if k in UNITS and isinstance(v, int | float)}
    total = 0.0

    inp = u.get("input_tokens", 0.0)
    cached = u.get("cached_input_tokens", 0.0)
    cache_write = u.get("cache_write_tokens", 0.0)
    audio_in = u.get("input_audio_tokens", 0.0)
    p_in = p.get("input_tokens", 0.0)
    text_in = max(inp - cached - cache_write - audio_in, 0.0)
    total += text_in * p_in
    total += cached * p.get("cached_input_tokens", p_in)
    total += cache_write * p.get("cache_write_tokens", p_in)
    total += audio_in * p.get("input_audio_tokens", p_in)

    out = u.get("output_tokens", 0.0)
    audio_out = u.get("output_audio_tokens", 0.0)
    p_out = p.get("output_tokens", 0.0)
    total += max(out - audio_out, 0.0) * p_out
    total += audio_out * p.get("output_audio_tokens", p_out)

    # Per-second prices apply only when the call was not metered in audio tokens.
    if not audio_in:
        total += u.get("input_audio_seconds", 0.0) * p.get("input_audio_seconds", 0.0)
    if not audio_out:
        total += u.get("output_audio_seconds", 0.0) * p.get("output_audio_seconds", 0.0)

    for unit in (
        "input_characters",
        "output_characters",
        "input_images",
        "output_images",
        "requests",
    ):
        total += u.get(unit, 0.0) * p.get(unit, 0.0)
    return total
