"""Auto-instrumentation for model SDKs. Use ``tirkit.instrument()``."""

from __future__ import annotations

from collections.abc import Callable

from tirkit.instrumentation._patch import unpatch_all


def _registry() -> dict[str, Callable[[], bool]]:
    from tirkit.instrumentation import (
        anthropic,
        bedrock,
        cohere,
        gemini,
        litellm,
        mistral,
        openai,
        vertex,
    )

    def transformers() -> bool:
        from tirkit.integrations.transformers import instrument as hf

        return hf()

    def integration(module: str) -> Callable[[], bool]:
        def run() -> bool:
            import importlib

            return bool(importlib.import_module(f"tirkit.integrations.{module}").instrument())

        return run

    return {
        "openai": openai.instrument,
        "anthropic": anthropic.instrument,
        "bedrock": bedrock.instrument,
        "cohere": cohere.instrument,
        "gemini": gemini.instrument,
        "litellm": litellm.instrument,
        "mistral": mistral.instrument,
        "vertex": vertex.instrument,
        "transformers": transformers,
        "openai_agents": integration("openai_agents"),
    }


def instrument(*providers: str) -> list[str]:
    """Patch the named providers (default: every supported SDK that is installed)."""
    registry = _registry()
    names = providers or tuple(registry)
    unknown = set(names) - set(registry)
    if unknown:
        raise ValueError(f"unknown providers: {sorted(unknown)}; known: {sorted(registry)}")
    return [name for name in names if registry[name]()]


def uninstrument() -> None:
    """Restore every patched SDK method."""
    unpatch_all()
