"""Core data types shared by every layer: spans, traces, examples, datasets, scores, runs.

This module sits in the core layer (see docs/DECISIONS.md, "Layers"). It imports only the standard
library and other core modules, so storage, the SDK and evaluation code can all depend on it
without depending on each other. The types are re-exported from their historical homes
(``tirkit.tracing.Span``, ``tirkit.datasets.Dataset``, ``tirkit.runner.RunResult`` and so on).
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tirkit import stats
from tirkit.serialize import to_jsonable


@dataclass
class Span:
    """One unit of work. ``start``/``end`` are Unix seconds."""

    name: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: str | None = None
    start: float = field(default_factory=time.time)
    end: float | None = None
    kind: str = "generic"
    status: str = "ok"  # ok | error
    level: str = "default"  # debug | default | warning | error
    model: str | None = None
    model_params: dict[str, Any] = field(default_factory=dict)
    input: Any = None
    output: Any = None
    usage: dict[str, float] = field(default_factory=dict)
    cost_usd: float | None = None
    error: str | None = None
    # Trace-level fields, inherited by children.
    session_id: str | None = None
    user_id: str | None = None
    release: str | None = None
    tags: list[str] = field(default_factory=list)
    prompt_version: str | None = None
    # Latency details: time to first token / first audio byte (ms), for streams.
    ttft_ms: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float | None:
        return None if self.end is None else (self.end - self.start) * 1000

    @property
    def input_tokens(self) -> int | None:
        v = self.usage.get("input_tokens")
        return None if v is None else int(v)

    @property
    def output_tokens(self) -> int | None:
        v = self.usage.get("output_tokens")
        return None if v is None else int(v)

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    def set_usage(self, **units: float | int | None) -> None:
        """Record metered usage (see :mod:`tirkit.pricing` for unit names)."""
        for k, v in units.items():
            if v is not None:
                self.usage[k] = float(v)

    def set_attributes(self, **attrs: Any) -> None:
        self.attributes.update(attrs)

    def mark_first_token(self, at: float | None = None) -> None:
        """Record time to first token (or first audio byte) once, for streamed output."""
        if self.ttft_ms is None:
            self.ttft_ms = ((at if at is not None else time.time()) - self.start) * 1000

    def record_error(self, exc: BaseException | str) -> None:
        self.status = "error"
        self.level = "error"
        self.error = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Span:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Trace:
    """Summary of one trace (aggregated over its spans)."""

    trace_id: str
    name: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    release: str | None = None
    tags: list[str] = field(default_factory=list)
    input: Any = None
    output: Any = None
    start: float | None = None
    end: float | None = None
    duration_ms: float | None = None
    n_spans: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    has_error: bool = False


@dataclass
class Example:
    input: dict[str, Any]
    expected: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Dataset:
    name: str
    examples: list[Example]

    @property
    def version(self) -> str:
        """Content hash: the same examples always give the same version."""
        payload = json.dumps([asdict(e) for e in self.examples], sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def __len__(self) -> int:
        return len(self.examples)

    def __iter__(self) -> Any:
        return iter(self.examples)

    # -- IO -------------------------------------------------------------------------------------

    def to_jsonl(self, path: str | Path) -> None:
        """One example per line: ``{"input": ..., "expected": ..., "metadata": ...}``."""
        with Path(path).open("w", encoding="utf-8") as f:
            for e in self.examples:
                f.write(json.dumps(asdict(e), ensure_ascii=False, default=str) + "\n")

    @classmethod
    def from_jsonl(cls, path: str | Path, name: str | None = None) -> Dataset:
        examples = []
        with Path(path).open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise ValueError(f"{path}:{lineno}: invalid JSON") from exc
                examples.append(_example_from_row(row))
        return cls(name or Path(path).stem, examples)

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        name: str | None = None,
        *,
        input_columns: list[str] | None = None,
        expected_column: str | None = "expected",
    ) -> Dataset:
        """Each row becomes an example. Without ``input_columns`` every column except the
        expected column (and ``metadata.*`` columns) is an input field."""
        examples = []
        with Path(path).open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                meta = {k[9:]: v for k, v in row.items() if k.startswith("metadata.")}
                cols = input_columns or [
                    k for k in row if k != expected_column and not k.startswith("metadata.")
                ]
                expected = row.get(expected_column) if expected_column else None
                examples.append(Example({k: row[k] for k in cols}, expected or None, meta))
        return cls(name or Path(path).stem, examples)


def _example_from_row(row: Any) -> Example:
    if isinstance(row, dict) and "input" in row:
        inp = row["input"] if isinstance(row["input"], dict) else {"input": row["input"]}
        return Example(inp, row.get("expected"), row.get("metadata") or {})
    if isinstance(row, dict):
        expected = row.pop("expected", None)
        return Example(row, expected)
    return Example({"input": row})


@dataclass
class Score:
    value: float | None
    reason: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.value is not None and self.error is None


@dataclass
class RunResult:
    dataset: str
    dataset_version: str
    outputs: list[Any] = field(default_factory=list)
    scores: dict[str, list[float | None]] = field(default_factory=dict)  # scorer -> per example
    experiment_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str | None = None
    target: str | None = None
    errors: list[str | None] = field(default_factory=list)
    latencies_ms: list[float | None] = field(default_factory=list)
    costs_usd: list[float | None] = field(default_factory=list)
    trace_ids: list[str | None] = field(default_factory=list)
    reasons: dict[str, list[str | None]] = field(default_factory=dict)
    scorer_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.time)

    def higher_is_better(self, scorer: str) -> bool:
        return bool(self.scorer_info.get(scorer, {}).get("higher_is_better", True))

    def summary(self, *, seed: int = 0, n_resamples: int = 10_000) -> dict[str, dict[str, Any]]:
        """Per scorer: mean, 95% CI, number scored and number failed."""
        out: dict[str, dict[str, Any]] = {}
        for name, values in self.scores.items():
            ok = [v for v in values if v is not None and not math.isnan(v)]
            row: dict[str, Any] = {"n": len(ok), "failed": len(values) - len(ok)}
            if ok:
                row["mean"] = stats.mean(ok)
                row["ci"] = stats.bootstrap_ci(ok, seed=seed, n_resamples=n_resamples)
            out[name] = row
        return out

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["outputs"] = to_jsonable(self.outputs)
        return data

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=1, default=str))

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RunResult:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def load_json(cls, path: str | Path) -> RunResult:
        return cls.from_json(json.loads(Path(path).read_text()))
