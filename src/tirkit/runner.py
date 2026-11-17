"""Run a dataset through a target and score it; compare runs; gate deploys (SPEC FR-14–16, §6)."""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from tirkit import stats
from tirkit.scorers import Score, Scorer, higher_is_better, scorer_version
from tirkit.tracing import copy_context_run, span
from tirkit.types import Dataset, Example
from tirkit.types import RunResult as RunResult


def _call_target(target: Callable[..., Any], inp: dict[str, Any]) -> Any:
    result = target(inp)
    if inspect.isawaitable(result):
        return asyncio.run(_await(result))
    return result


async def _await(x: Any) -> Any:
    return await x


def _item(
    idx: int,
    example: Example,
    target: Callable[..., Any],
    retries: int,
    experiment_id: str,
    target_name: str,
) -> tuple[Any, str | None, float, str]:
    with span(
        f"experiment:{target_name}",
        kind="chain",
        input=example.input,
        experiment_id=experiment_id,
        item_idx=idx,
    ) as s:
        error: str | None = None
        output: Any = None
        t0 = time.perf_counter()
        for attempt in range(retries + 1):
            try:
                output = _call_target(target, example.input)
                error = None
                break
            except Exception as exc:  # noqa: BLE001 - recorded per item
                error = f"{type(exc).__name__}: {exc}"
                if attempt < retries:
                    time.sleep(min(2**attempt, 10))
        latency = (time.perf_counter() - t0) * 1000
        s.output = output
        if error:
            s.record_error(error)
        return output, error, latency, s.trace_id


def run(
    dataset: Dataset,
    target: Callable[[dict[str, Any]], Any],
    scorers: Sequence[Scorer],
    *,
    name: str | None = None,
    store: Any = None,
    concurrency: int = 4,
    retries: int = 0,
    config: dict[str, Any] | None = None,
) -> RunResult:
    """Call ``target`` on every example, score every output with every scorer, trace each call.

    Errors are recorded per item (output ``None``, scores ``None``) and never abort the run. With
    ``store`` the experiment, outputs and scores are saved and per-item cost is read from traces.
    """
    target_name = getattr(target, "__qualname__", getattr(target, "__name__", "target"))
    result = RunResult(
        dataset=dataset.name,
        dataset_version=dataset.version,
        name=name,
        target=f"{getattr(target, '__module__', '')}:{target_name}",
        config=dict(config or {}),
    )
    n = len(dataset.examples)
    items: list[tuple[Any, str | None, float, str] | None] = [None] * n
    work = [
        (i, e, target, retries, result.experiment_id, target_name)
        for i, e in enumerate(dataset.examples)
    ]
    if concurrency <= 1:
        for i, *rest in work:
            items[i] = _item(i, *rest)  # type: ignore[arg-type]
    else:
        with ThreadPoolExecutor(concurrency) as pool:
            futures = {pool.submit(copy_context_run(_item), *w): w[0] for w in work}
            for fut, i in futures.items():
                items[i] = fut.result()
    for item in items:
        assert item is not None
        output, error, latency, trace_id = item
        result.outputs.append(output)
        result.errors.append(error)
        result.latencies_ms.append(latency)
        result.trace_ids.append(trace_id)

    for scorer in scorers:
        values: list[float | None] = []
        reasons: list[str | None] = []
        for example, output, error in zip(
            dataset.examples, result.outputs, result.errors, strict=True
        ):
            if error is not None:
                values.append(None)
                reasons.append(f"target failed: {error}")
                continue
            try:
                sc = scorer.score(example, output)
            except Exception as exc:  # noqa: BLE001
                sc = Score(None, error=f"{type(exc).__name__}: {exc}")
            values.append(sc.value if sc.ok else None)
            reasons.append(sc.reason if sc.ok else sc.error)
        result.scores[scorer.name] = values
        result.reasons[scorer.name] = reasons
        result.scorer_info[scorer.name] = {
            "version": scorer_version(scorer),
            "higher_is_better": higher_is_better(scorer),
        }

    if store is not None:
        from tirkit.client import flush

        flush()
        for tid in result.trace_ids:
            t = store.get_trace(tid) if tid else None
            result.costs_usd.append(t.cost_usd if t else None)
        store.save_experiment(result)
    return result


@dataclass
class ScorerComparison:
    scorer: str
    n: int
    excluded: int
    mean_baseline: float
    mean_candidate: float
    ci_baseline: tuple[float, float]
    ci_candidate: tuple[float, float]
    diff: float  # candidate - baseline
    diff_ci: tuple[float, float]
    higher_is_better: bool

    @property
    def verdict(self) -> str:
        lo, hi = self.diff_ci
        if lo > 0:
            return "better" if self.higher_is_better else "worse"
        if hi < 0:
            return "worse" if self.higher_is_better else "better"
        return "no clear difference"


def _paired(
    baseline: RunResult, candidate: RunResult, scorer: str
) -> tuple[list[float], list[float], int]:
    if baseline.dataset_version != candidate.dataset_version:
        raise ValueError(
            "refusing to compare runs on different dataset versions: "
            f"{baseline.dataset}@{baseline.dataset_version} vs "
            f"{candidate.dataset}@{candidate.dataset_version}"
        )
    a = baseline.scores.get(scorer)
    b = candidate.scores.get(scorer)
    if a is None or b is None:
        raise KeyError(f"scorer {scorer!r} missing from one of the runs")
    if len(a) != len(b):
        raise ValueError("runs have different numbers of items")
    pa, pb = [], []
    for x, y in zip(a, b, strict=True):
        if x is None or y is None or math.isnan(x) or math.isnan(y):
            continue
        pa.append(float(x))
        pb.append(float(y))
    return pa, pb, len(a) - len(pa)


def compare(
    baseline: RunResult,
    candidate: RunResult,
    *,
    scorers: Sequence[str] | None = None,
    seed: int = 0,
    n_resamples: int = 10_000,
) -> list[ScorerComparison]:
    """Per scorer: each run's mean with a 95% CI, and the paired difference (candidate −
    baseline) with its 95% bootstrap CI. Items that failed in either run are excluded and counted.
    """
    names = list(scorers or [s for s in baseline.scores if s in candidate.scores])
    out = []
    for name in names:
        a, b, excluded = _paired(baseline, candidate, name)
        if not a:
            raise ValueError(f"no items scored by {name!r} in both runs")
        out.append(
            ScorerComparison(
                scorer=name,
                n=len(a),
                excluded=excluded,
                mean_baseline=stats.mean(a),
                mean_candidate=stats.mean(b),
                ci_baseline=stats.bootstrap_ci(a, seed=seed, n_resamples=n_resamples),
                ci_candidate=stats.bootstrap_ci(b, seed=seed, n_resamples=n_resamples),
                diff=stats.mean(b) - stats.mean(a),
                diff_ci=stats.paired_bootstrap_diff(b, a, seed=seed, n_resamples=n_resamples),
                higher_is_better=baseline.higher_is_better(name),
            )
        )
    return out


def gate(
    baseline: RunResult,
    candidate: RunResult,
    scorer: str,
    *,
    max_drop: float = 0.0,
    seed: int = 0,
    n_resamples: int = 10_000,
) -> bool:
    """Pass unless the candidate is confidently worse than baseline by more than max_drop.

    d = candidate − baseline per example (sign flipped for lower-is-better scorers such as WER).
    Fails only if the upper bound of the 95% CI of mean(d) is below −max_drop (SPEC §6.3).
    """
    a, b, _ = _paired(baseline, candidate, scorer)
    if not a:
        raise ValueError(f"no items scored by {scorer!r} in both runs")
    if not baseline.higher_is_better(scorer):
        a, b = [-x for x in a], [-y for y in b]
    _, hi = stats.paired_bootstrap_diff(b, a, seed=seed, n_resamples=n_resamples)
    return not hi < -max_drop
