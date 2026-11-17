"""Versioned eval datasets, including ones mined from production traces (SPEC FR-12, §6.6)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from tirkit.redact import redact_obj
from tirkit.serialize import to_jsonable
from tirkit.types import Dataset as Dataset
from tirkit.types import Example as Example
from tirkit.types import Span
from tirkit.types import _example_from_row as _example_from_row

# -- mining traces ------------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    return _WS.sub(" ", text.lower()).strip()


def shingles(text: str, n: int = 5) -> set[tuple[str, ...]]:
    words = normalise(text).split(" ")
    if len(words) < n:
        return {tuple(words)}
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def jaccard(a: set[Any], b: set[Any]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _leaf_text(obj: Any) -> str:
    """All string and number leaves of a JSON-like tree, in key order, joined by spaces."""
    if isinstance(obj, dict):
        return " ".join(_leaf_text(obj[k]) for k in sorted(obj, key=str))
    if isinstance(obj, list | tuple):
        return " ".join(_leaf_text(v) for v in obj)
    if obj is None:
        return ""
    return str(obj)


def remove_duplicates(
    examples: list[Example], *, threshold: float = 0.9, key: Callable[[Example], str] | None = None
) -> list[Example]:
    """Drop exact duplicates (after normalising) and near-duplicates: word 5-gram Jaccard
    similarity ≥ ``threshold`` to an earlier kept example (SPEC §6.6)."""
    key = key or (lambda e: _leaf_text(e.input))
    kept: list[Example] = []
    seen_exact: set[str] = set()
    kept_shingles: list[set[tuple[str, ...]]] = []
    for e in examples:
        text = normalise(key(e))
        if text in seen_exact:
            continue
        sh = shingles(text)
        if any(
            # Jaccard ≥ t requires the smaller set to be ≥ t × the larger: skip hopeless pairs.
            min(len(sh), len(other)) >= threshold * max(len(sh), len(other))
            and jaccard(sh, other) >= threshold
            for other in kept_shingles
        ):
            continue
        seen_exact.add(text)
        kept_shingles.append(sh)
        kept.append(e)
    return kept


def from_traces(
    spans: Iterable[Span],
    *,
    name: str = "from-traces",
    dedupe: bool = True,
    redact: bool = True,
    threshold: float = 0.9,
    expected_from_output: bool = False,
    input_fn: Callable[[Span], dict[str, Any] | None] | None = None,
    extra_patterns: Iterable[tuple[str, str]] = (),
) -> Dataset:
    """Mine test cases from production spans.

    Each span with an input becomes an example: ``input`` from the span input (wrapped as
    ``{"input": ...}`` when not a dict, or chosen by ``input_fn``), the span's output kept as
    ``metadata["reference_output"]`` (or as ``expected`` with ``expected_from_output=True``, for
    regression sets where the current answer is the reference). PII is redacted *before* anything
    is returned, so it can never be saved unredacted. Media references are kept.
    """
    examples: list[Example] = []
    for s in spans:
        inp = input_fn(s) if input_fn else s.input
        if inp is None:
            continue
        inp = to_jsonable(inp)
        if not isinstance(inp, dict):
            inp = {"input": inp}
        out = to_jsonable(s.output)
        meta: dict[str, Any] = {
            "source_trace_id": s.trace_id,
            "source_span_id": s.span_id,
            "model": s.model,
            "kind": s.kind,
        }
        expected = None
        if expected_from_output:
            expected = out
        elif out is not None:
            meta["reference_output"] = out
        if redact:
            inp = redact_obj(inp, extra_patterns)
            expected = redact_obj(expected, extra_patterns)
            meta = redact_obj(meta, extra_patterns)
        examples.append(Example(inp, expected, meta))
    if dedupe:
        examples = remove_duplicates(examples, threshold=threshold)
    return Dataset(name, examples)
