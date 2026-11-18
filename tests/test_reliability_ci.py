"""The kappa confidence interval in a reliability report."""

from __future__ import annotations

import pytest

from tirkit import stats
from tirkit.reliability import reliability_report


def test_kappa_ci_contains_the_estimate_and_is_reproducible() -> None:
    a = [1, 2, 3, 4, 5] * 8
    b = [1, 2, 3, 4, 4] * 8
    lo, hi = stats.kappa_ci(a, b)
    assert lo <= stats.cohen_kappa(a, b) <= hi
    assert (lo, hi) == stats.kappa_ci(a, b)


def test_kappa_ci_is_wider_with_fewer_items() -> None:
    a = [1, 2, 3, 4, 5] * 20
    b = [1, 2, 3, 4, 4] * 20
    lo_big, hi_big = stats.kappa_ci(a, b)
    lo_small, hi_small = stats.kappa_ci(a[:15], b[:15])
    assert (hi_small - lo_small) > (hi_big - lo_big)


def test_kappa_ci_needs_items() -> None:
    with pytest.raises(ValueError):
        stats.kappa_ci([], [])


def test_report_carries_the_interval_only_when_something_was_judged() -> None:
    rows = [
        {
            "input": {},
            "output": "x",
            "labels": {"s": 1 + i % 5},
            "judge": 1 + (i + (i % 3 == 0)) % 5,
        }
        for i in range(30)
    ]
    rep = reliability_report(rows, human_key="s")
    assert rep["kappa_ci_low"] <= rep["kappa"] <= rep["kappa_ci_high"]
    empty = reliability_report([], human_key="s")
    assert "kappa_ci_low" not in empty
