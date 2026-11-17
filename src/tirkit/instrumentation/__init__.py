"""Auto-instrumentation for model SDKs. Use ``tirkit.instrument()``."""

from __future__ import annotations

from collections.abc import Callable

from tirkit.instrumentation._patch import unpatch_all


def _registry() -> dict[str, Callable[[], bool]]:
    from tirkit.instrumentation import anthropic, gemini, openai

    def transformers() -> bool:
        from tirkit.integrations.transformers import instrument as hf

        return hf()

    return {
        "openai": openai.instrument,
        "anthropic": anthropic.instrument,
        "gemini": gemini.instrument,
        "transformers": transformers,
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
