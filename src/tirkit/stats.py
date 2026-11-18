"""Statistics for honest comparisons. Definitions in SPEC.md section 6.

Pure standard library and seeded, so every number is reproducible (NFR-4).
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable, Hashable, Sequence


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


def bootstrap_intervals(
    n: int,
    statistic: Callable[[list[int]], Sequence[float]],
    *,
    n_resamples: int = 2_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> list[tuple[float, float]]:
    """Percentile bootstrap CIs for statistics of ``n`` items computed together.

    ``statistic`` receives resampled item indices and returns one value per statistic; each
    statistic gets its own interval, but all share the same resamples (so a difference between
    two of them is paired)."""
    if n <= 0:
        raise ValueError("bootstrap of no items")
    rng = random.Random(seed)
    draws: list[Sequence[float]] = []
    for _ in range(n_resamples):
        draws.append(statistic(rng.choices(range(n), k=n)))
    out = []
    for j in range(len(draws[0])):
        values = sorted(d[j] for d in draws)
        out.append((_percentile(values, alpha / 2), _percentile(values, 1 - alpha / 2)))
    return out


def bootstrap_stat(
    n: int,
    statistic: Callable[[list[int]], float],
    *,
    n_resamples: int = 2_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap CI for one statistic of ``n`` items (resampled with replacement)."""
    return bootstrap_intervals(
        n, lambda idx: (statistic(idx),), n_resamples=n_resamples, alpha=alpha, seed=seed
    )[0]


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
    return bootstrap_stat(
        n,
        lambda idx: cohen_kappa([rater_a[i] for i in idx], [rater_b[i] for i in idx]),
        n_resamples=n_resamples,
        alpha=alpha,
        seed=seed,
    )


Z_95 = 1.959963984540054

# Two-sided 95% critical values of Student's t by degrees of freedom (standard table).
_T_95 = {
    1: 12.7062, 2: 4.3027, 3: 3.1824, 4: 2.7764, 5: 2.5706, 6: 2.4469, 7: 2.3646, 8: 2.3060,
    9: 2.2622, 10: 2.2281, 11: 2.2010, 12: 2.1788, 13: 2.1604, 14: 2.1448, 15: 2.1314,
    16: 2.1199, 17: 2.1098, 18: 2.1009, 19: 2.0930, 20: 2.0860, 21: 2.0796, 22: 2.0739,
    23: 2.0687, 24: 2.0639, 25: 2.0595, 26: 2.0555, 27: 2.0518, 28: 2.0484, 29: 2.0452,
    30: 2.0423, 40: 2.0211, 60: 2.0003, 120: 1.9799,
}


def t_critical_95(df: int) -> float:
    """Two-sided 95% t critical value. Between table rows the smaller df is used, so the
    interval errs wide, never narrow; above 120 df the normal value is used."""
    if df < 1:
        raise ValueError("t needs at least one degree of freedom")
    if df > 120:
        return Z_95
    return _T_95[max(k for k in _T_95 if k <= df)]


def mean_ci_t(n: int, mean_value: float, sd: float) -> tuple[float, float] | None:
    """95% t interval for a mean from its sample size, mean and sample standard deviation.
    None below two values (no spread to measure)."""
    if n < 2:
        return None
    half = t_critical_95(n - 1) * sd / math.sqrt(n)
    return mean_value - half, mean_value + half


def t_interval(values: Sequence[float]) -> tuple[float, float] | None:
    """95% t interval for the mean of ``values`` (None below two values)."""
    n = len(values)
    if n < 2:
        return None
    m = mean(values)
    sd = math.sqrt(math.fsum((v - m) ** 2 for v in values) / (n - 1))
    return mean_ci_t(n, m, sd)


def wilson_ci(k: int, n: int, *, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a proportion k/n: sound near 0 and 1 and at small n."""
    if n <= 0:
        raise ValueError("a proportion needs at least one trial")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


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
