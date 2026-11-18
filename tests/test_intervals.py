"""Every compared number carries an interval: judge reports, dashboard score trends, score alerts.

Reference values are from the textbook formulas (Wilson 1927; Student's t table), so the tests
check the statistics are right, not only present.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tirkit import stats
from tirkit.reliability import reliability_report, render_reliability

# -- statistics -------------------------------------------------------------------------------


def test_wilson_matches_reference_values() -> None:
    lo, hi = stats.wilson_ci(0, 10)
    assert lo == pytest.approx(0.0, abs=1e-9) and hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = stats.wilson_ci(30, 46)
    assert lo == pytest.approx(0.5077, abs=1e-4) and hi == pytest.approx(0.7732, abs=1e-4)
    with pytest.raises(ValueError):
        stats.wilson_ci(0, 0)


def test_t_interval_matches_reference_values() -> None:
    lo, hi = stats.t_interval([1, 2, 3, 4, 5])  # mean 3, sd 1.5811, t(4) = 2.7764
    assert lo == pytest.approx(1.0368, abs=1e-4) and hi == pytest.approx(4.9632, abs=1e-4)
    assert stats.t_critical_95(1) == pytest.approx(12.7062, abs=1e-4)
    assert stats.t_critical_95(10_000) == pytest.approx(1.96, abs=1e-2)
    # Between table rows the smaller df is used, so the interval is never too narrow.
    assert stats.t_critical_95(45) == stats.t_critical_95(40)
    assert stats.mean_ci_t(1, 0.5, 0.0) is None


def test_bootstrap_stat_is_reproducible_and_kappa_ci_unchanged() -> None:
    a = [1, 2, 3, 4, 5] * 8
    b = [1, 2, 3, 4, 4] * 8
    via_generic = stats.bootstrap_stat(
        len(a), lambda idx: stats.cohen_kappa([a[i] for i in idx], [b[i] for i in idx])
    )
    assert via_generic == stats.kappa_ci(a, b)


# -- judge reliability report -----------------------------------------------------------------

ESTIMATES = [
    "kappa",
    "weighted_kappa",
    "pass_fail_kappa",
    "exact_agreement",
    "length_corr_judge",
    "length_corr_human",
    "length_bias",
    "flip_rate",
]


def _scored(n: int = 40) -> list[dict[str, Any]]:
    rows = [
        {
            "input": {},
            "output": " ".join(["word"] * (3 + i % 7)),
            "labels": {"s": 1 + i % 5},
            "judge": 1 + (i + (i % 3 == 0)) % 5,
        }
        for i in range(n)
    ]
    rows[0]["_pairs"] = [
        {"first": "a", "second": "b" if i % 3 == 0 else "a", "flipped": i % 3 == 0}
        for i in range(30)
    ]
    return rows


def test_every_reported_statistic_has_an_interval_that_contains_it() -> None:
    rep = reliability_report(_scored(), human_key="s")
    for key in ESTIMATES:
        assert rep.get(key) is not None, key
        lo, hi = rep[f"{key}_ci_low"], rep[f"{key}_ci_high"]
        assert lo <= rep[key] <= hi, (key, lo, rep[key], hi)


def test_length_bias_is_judge_minus_human() -> None:
    rep = reliability_report(_scored(), human_key="s")
    assert rep["length_bias"] == pytest.approx(rep["length_corr_judge"] - rep["length_corr_human"])


def test_flip_rate_interval_is_wilson_and_stored() -> None:
    rep = reliability_report(_scored(), human_key="s")
    assert rep["n_pairs_decided"] == 30
    assert (rep["flip_rate_ci_low"], rep["flip_rate_ci_high"]) == stats.wilson_ci(10, 30)


def test_no_intervals_without_data() -> None:
    rep = reliability_report([], human_key="s")
    assert not any(k.endswith("_ci_low") for k in rep)


def test_cli_report_prints_the_intervals() -> None:
    text = render_reliability(reliability_report(_scored(), human_key="s"))
    assert text.count("95%") >= 5


# -- dashboard score trends -------------------------------------------------------------------


def test_score_trend_points_carry_a_t_interval(store: Any) -> None:
    now = time.time()
    for v in [1, 2, 3, 4, 5]:
        store.save_score("quality", float(v), source="llm_judge")
    store.save_score("single", 1.0, source="llm_judge")
    m = store.metrics(now - 3600, now + 60, buckets=1)
    (pt,) = m["scores"]["quality"]
    assert pt["n"] == 5 and pt["mean"] == pytest.approx(3.0)
    assert pt["ci_low"] == pytest.approx(1.0368, abs=1e-4)
    assert pt["ci_high"] == pytest.approx(4.9632, abs=1e-4)
    (one,) = m["scores"]["single"]
    assert one["ci_low"] is None and one["ci_high"] is None


# -- score alerts -----------------------------------------------------------------------------


def _alert(store: Any, url: str, threshold: float) -> Any:
    from tirkit.alerts import Alerts

    a = Alerts(store)
    a.create(
        "quality drop",
        {
            "metric": "score_mean",
            "score_name": "quality",
            "op": "<",
            "threshold": threshold,
            "window_s": 600,
        },
        {"type": "webhook", "url": url},
    )
    return a


def test_score_alert_waits_until_the_interval_is_past_the_threshold(store, webhook) -> None:
    url, received = webhook
    a = _alert(store, url, 0.5)
    for v in [0.0, 0.9]:  # mean 0.45 is below 0.5, but two scores prove nothing
        store.save_score("quality", v, source="llm_judge")
    assert a.check(a.items()[0]) == "ok" and not received
    for _ in range(20):
        store.save_score("quality", 0.1, source="llm_judge")
    assert a.check(a.items()[0]) == "firing"
    assert "95% interval" in received[0]["text"] and "22 scores" in received[0]["text"]
