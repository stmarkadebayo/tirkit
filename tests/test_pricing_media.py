"""Price lookup and cost for every unit; media extraction."""

from __future__ import annotations

import base64

import pytest

from tirkit import pricing
from tirkit.media import Attachment, MemoryMediaStore, extract_media, sniff_mime

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 2000


def test_lookup_exact_prefix_and_provider() -> None:
    assert pricing.lookup_price("gpt-4o") is not None
    assert pricing.lookup_price("gpt-4o-2099-01-01") == pricing.lookup_price("gpt-4o")
    assert pricing.lookup_price("openai/gpt-4o-mini") == pricing.lookup_price("gpt-4o-mini")
    assert pricing.lookup_price("totally-unknown-model-xyz") is None


def test_token_cost_with_cache() -> None:
    pricing.register_price(
        "t-model",
        input_tokens=1.0,
        output_tokens=2.0,
        cached_input_tokens=0.1,
        cache_write_tokens=1.25,
    )
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 50,
        "cache_write_tokens": 10,
        "output_tokens": 5,
    }
    assert pricing.cost("t-model", usage) == pytest.approx(40 * 1 + 50 * 0.1 + 10 * 1.25 + 5 * 2)


def test_speech_and_image_units() -> None:
    pricing.register_price("asr", input_audio_seconds=0.001)
    pricing.register_price("tts", input_characters=0.00001)
    pricing.register_price("img", output_images=0.04)
    assert pricing.cost("asr", {"input_audio_seconds": 60}) == pytest.approx(0.06)
    assert pricing.cost("tts", {"input_characters": 1000}) == pytest.approx(0.01)
    assert pricing.cost("img", {"output_images": 2}) == pytest.approx(0.08)


def test_audio_tokens_take_precedence_over_seconds() -> None:
    pricing.register_price(
        "rt", input_tokens=1.0, input_audio_tokens=10.0, input_audio_seconds=100.0
    )
    assert pricing.cost(
        "rt", {"input_tokens": 3, "input_audio_tokens": 2, "input_audio_seconds": 1}
    ) == pytest.approx(1 + 20)


def test_unknown_units_rejected() -> None:
    with pytest.raises(ValueError):
        pricing.register_price("x", per_banana=1.0)


def test_sniff() -> None:
    assert sniff_mime(PNG) == "image/png"
    assert sniff_mime(b"RIFF\0\0\0\0WAVEfmt ") == "audio/wav"
    assert sniff_mime(b"ID3\x04") == "audio/mpeg"


def test_extract_media_from_provider_shapes() -> None:
    b64 = base64.b64encode(PNG).decode()
    store = MemoryMediaStore()
    tree = {
        "openai_image": {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        "anthropic_image": {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        },
        "audio": {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
        "raw": PNG,
        "attachment": Attachment(data=PNG, mime="image/png"),
        "text": "keep me",
    }
    out = extract_media(tree, store)
    refs = [
        out["openai_image"]["image_url"]["url"],
        out["anthropic_image"]["source"],
        out["audio"]["input_audio"]["data"],
        out["raw"],
        out["attachment"],
    ]
    assert all(r["type"] == "media" and r["size"] == len(PNG) for r in refs)
    assert out["audio"]["input_audio"]["data"]["mime"] == "audio/wav"
    assert len(store.blobs) == 1  # content-addressed: stored once
    assert out["text"] == "keep me"
