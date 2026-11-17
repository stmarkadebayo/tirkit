"""Refresh src/tirkit/data/prices.json from LiteLLM's community-maintained price map (MIT licence).

Run: python scripts/update_prices.py
Keeps only the fields Tirkit prices, for every model mode Tirkit traces (chat, embeddings, images,
speech-to-text, text-to-speech, rerank, realtime). Prices are USD per unit.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

SOURCE = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
OUT = Path(__file__).resolve().parents[1] / "src" / "tirkit" / "data" / "prices.json"

# LiteLLM field -> Tirkit usage unit
FIELDS = {
    "input_cost_per_token": "input_tokens",
    "output_cost_per_token": "output_tokens",
    "cache_read_input_token_cost": "cached_input_tokens",
    "cache_creation_input_token_cost": "cache_write_tokens",
    "input_cost_per_audio_token": "input_audio_tokens",
    "output_cost_per_audio_token": "output_audio_tokens",
    "input_cost_per_second": "input_audio_seconds",
    "output_cost_per_second": "output_audio_seconds",
    "input_cost_per_character": "input_characters",
    "output_cost_per_character": "output_characters",
    "input_cost_per_image": "input_images",
    "output_cost_per_image": "output_images",
    "input_cost_per_query": "requests",
}


def main() -> int:
    with urllib.request.urlopen(SOURCE, timeout=60) as resp:  # noqa: S310 (fixed https URL)
        raw = json.load(resp)
    models: dict[str, dict[str, object]] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict) or name == "sample_spec":
            continue
        prices = {
            unit: float(entry[field])
            for field, unit in FIELDS.items()
            if isinstance(entry.get(field), int | float) and entry[field] > 0
        }
        if not prices:
            continue
        models[name] = {
            "mode": entry.get("mode"),
            "provider": entry.get("litellm_provider"),
            "prices": prices,
        }
    OUT.write_text(
        json.dumps(
            {
                "source": SOURCE,
                "licence": "MIT, Copyright (c) 2023 Berri AI",
                "retrieved": date.today().isoformat(),
                "models": dict(sorted(models.items())),
            },
            indent=0,
            sort_keys=False,
        )
        + "\n"
    )
    print(f"wrote {len(models)} models to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
