"""Statistics for honest comparisons. Definitions in SPEC.md section 6.

Pure standard library and seeded, so every number is reproducible (NFR-4).
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Hashable, Sequence


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("mean of empty sequence")
    return math.fsum(values) / len(values)


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of pre-sorted values, q in [0, 1]."""
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def bootstrap_means(
    values: Sequence[float], *, n_resamples: int = 10_000, seed: int = 0
) -> list[float]:
    """Means of ``n_resamples`` resamples (with replacement), sorted ascending."""
    vals = list(values)
    n = len(vals)
    if n == 0:
        raise ValueError("bootstrap of empty sequence")
    rng = random.Random(seed)
    choices = rng.choices
    out = [math.fsum(choices(vals, k=n)) / n for _ in range(n_resamples)]
    out.sort()
    return out


def bootstrap_ci(
    values: Sequence[float], *, n_resamples: int = 10_000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for the mean (SPEC §6.1)."""
    means = bootstrap_means(values, n_resamples=n_resamples, seed=seed)
    return _percentile(means, alpha / 2), _percentile(means, 1 - alpha / 2)


def paired_bootstrap_diff(
    a: Sequence[float],
    b: Sequence[float],
    *,
    n_resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """CI for mean(a) - mean(b) on the same examples (SPEC §6.2): bootstrap of d_i = a_i - b_i."""
    if len(a) != len(b):
        raise ValueError(f"paired samples differ in length: {len(a)} vs {len(b)}")
    return bootstrap_ci(
        [x - y for x, y in zip(a, b, strict=True)],
        n_resamples=n_resamples,
        alpha=alpha,
        seed=seed,
    )


def cohen_kappa(rater_a: Sequence[Hashable], rater_b: Sequence[Hashable]) -> float:
    """Agreement between two raters beyond what chance would give (SPEC §6.4).

    κ = (p_o − p_e) / (1 − p_e). If p_e = 1, returns 1.0 when p_o = 1, else 0.0.
    """
    if len(rater_a) != len(rater_b):
        raise ValueError("raters labelled different numbers of items")
    n = len(rater_a)
    if n == 0:
        raise ValueError("no items")
    p_o = sum(x == y for x, y in zip(rater_a, rater_b, strict=True)) / n
    ca, cb = Counter(rater_a), Counter(rater_b)
    p_e = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    if p_e == 1:
        return 1.0 if p_o == 1 else 0.0
    return (p_o - p_e) / (1 - p_e)


def kappa_ci(
    rater_a: Sequence[Hashable],
    rater_b: Sequence[Hashable],
    *,
    n_resamples: int = 2_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap CI for Cohen's kappa: resample the labelled items with replacement."""
    n = len(rater_a)
    if n != len(rater_b) or n == 0:
        raise ValueError("need two equal, non-empty label lists")
    rng = random.Random(seed)
    values = []
    for _ in range(n_resamples):
        idx = rng.choices(range(n), k=n)
        values.append(cohen_kappa([rater_a[i] for i in idx], [rater_b[i] for i in idx]))
    values.sort()
    return _percentile(values, alpha / 2), _percentile(values, 1 - alpha / 2)


def weighted_kappa(
    rater_a: Sequence[int], rater_b: Sequence[int], *, categories: Sequence[int] | None = None
) -> float:
    """Quadratic-weighted kappa for ordinal labels (e.g. 1–5 scores): near-misses count partly."""
    if len(rater_a) != len(rater_b) or not rater_a:
        raise ValueError("need two equal, non-empty label lists")
    cats = sorted(set(categories or []) | set(rater_a) | set(rater_b))
    idx = {c: i for i, c in enumerate(cats)}
    k = len(cats)
    if k == 1:
        return 1.0
    n = len(rater_a)
    obs = [[0.0] * k for _ in range(k)]
    for x, y in zip(rater_a, rater_b, strict=True):
        obs[idx[x]][idx[y]] += 1
    ra = [sum(row) for row in obs]
    rb = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            w = ((i - j) ** 2) / ((k - 1) ** 2)
            num += w * obs[i][j]
            den += w * ra[i] * rb[j] / n
    return 1.0 - num / den if den else 1.0


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for t in range(i, j + 1):
            ranks[order[t]] = avg
        i = j + 1
    return ranks


def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    if len(x) != len(y) or len(x) < 2:
        raise ValueError("need two equal sequences of length >= 2")
    mx, my = mean(x), mean(y)
    sxy = math.fsum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = math.fsum((a - mx) ** 2 for a in x)
    syy = math.fsum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman rank correlation (ties get average ranks). Used for length bias (SPEC §6.5)."""
    return pearson(_ranks(x), _ranks(y))
