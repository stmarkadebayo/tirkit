"""Statistics, runner, compare, gate, reports and CLI (M3)."""

from __future__ import annotations

import json
import random

import pytest

from tirkit import stats
from tirkit.datasets import Dataset, Example
from tirkit.runner import RunResult, compare, gate, run
from tirkit.scorers import ExactMatch, WordErrorRate


def test_bootstrap_is_seeded_and_brackets_mean() -> None:
    rng = random.Random(1)
    vals = [rng.random() for _ in range(200)]
    assert stats.bootstrap_ci(vals, seed=3) == stats.bootstrap_ci(vals, seed=3)
    lo, hi = stats.bootstrap_ci(vals, seed=3)
    assert lo < stats.mean(vals) < hi


def test_paired_diff_detects_shift() -> None:
    a = [1.0] * 50 + [0.0] * 50
    b = [1.0] * 80 + [0.0] * 20
    lo, hi = stats.paired_bootstrap_diff(b, a)
    assert 0 < lo < 0.3 < hi


def test_kappa_edge_cases() -> None:
    assert stats.cohen_kappa([1, 1, 1], [1, 1, 1]) == 1.0  # p_e = 1, perfect
    assert stats.cohen_kappa(["a", "b"], ["b", "a"]) < 0
    assert stats.weighted_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)
    assert 0 < stats.weighted_kappa([1, 2, 3, 4, 5], [2, 2, 3, 4, 4]) < 1


def test_spearman() -> None:
    assert stats.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert stats.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert stats.spearman([1, 2, 2, 3], [1, 2, 3, 4]) > 0.9


def _ds(n: int = 30) -> Dataset:
    return Dataset("arith", [Example({"a": i, "b": i}, expected=2 * i) for i in range(n)])


def test_run_scores_errors_and_store_round_trip(store) -> None:
    import tirkit

    client = tirkit.init(path=store.path)

    def target(inp):
        if inp["a"] == 3:
            raise RuntimeError("flaky")
        return inp["a"] + inp["b"] + (1 if inp["a"] % 10 == 0 else 0)

    res = run(_ds(), target, [ExactMatch()], name="v1", store=client.store, concurrency=4)
    assert res.errors[3] and res.scores["exact_match"][3] is None
    assert res.scores["exact_match"][1] == 1.0 and res.scores["exact_match"][0] == 0.0
    assert all(t for t in res.trace_ids)
    back = client.store.load_experiment(res.experiment_id)
    assert back.scores == res.scores and back.dataset_version == res.dataset_version
    assert client.store.list_experiments()[0]["experiment_id"] == res.experiment_id
    summary = res.summary()["exact_match"]
    assert summary["failed"] == 1 and summary["n"] == 29
    client.shutdown()


def test_compare_refuses_different_versions() -> None:
    a = RunResult("d", "v1", scores={"s": [1.0]})
    b = RunResult("d", "v2", scores={"s": [1.0]})
    with pytest.raises(ValueError, match="different dataset versions"):
        compare(a, b)


def test_gate_passes_on_noise_and_respects_max_drop() -> None:
    rng = random.Random(0)
    base = [float(rng.random() < 0.8) for _ in range(100)]
    noisy = [x if rng.random() > 0.05 else 1 - x for x in base]
    a = RunResult("d", "v", scores={"s": base})
    b = RunResult("d", "v", scores={"s": noisy})
    assert gate(a, b, "s") is True
    worse = RunResult("d", "v", scores={"s": [0.0 if i < 20 else x for i, x in enumerate(base)]})
    assert gate(a, worse, "s") is False
    assert gate(a, worse, "s", max_drop=0.5) is True


def test_gate_lower_is_better_scorer() -> None:
    info = {"wer": {"higher_is_better": False}}
    base = RunResult("d", "v", scores={"wer": [0.1] * 60}, scorer_info=info)
    worse = RunResult("d", "v", scores={"wer": [0.4] * 60}, scorer_info=info)
    better = RunResult("d", "v", scores={"wer": [0.0] * 60}, scorer_info=info)
    assert gate(base, worse, "wer") is False
    assert gate(base, better, "wer") is True
    assert WordErrorRate.higher_is_better is False


def test_compare_report_and_json(tmp_path) -> None:
    from tirkit.report import compare_json, markdown_to_html, render_compare

    a = RunResult("d", "v", scores={"s": [1.0] * 50 + [0.0] * 50}, experiment_id="base")
    b = RunResult("d", "v", scores={"s": [1.0] * 90 + [0.0] * 10}, experiment_id="cand")
    comps = compare(a, b)
    assert comps[0].verdict == "better" and comps[0].diff == pytest.approx(0.4)
    md = render_compare(comps, a, b, gate_scorer="s", passed=True)
    assert "95% CI" in md and "passed" in md
    assert "<table>" in markdown_to_html(md)
    assert compare_json(comps)[0]["verdict"] == "better"
    a.save_json(tmp_path / "a.json")
    assert RunResult.load_json(tmp_path / "a.json").scores == a.scores


def test_cli_run_compare_gate(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from tirkit.cli import app

    pkg = tmp_path / "apps"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "calc.py").write_text(
        "def good(inp):\n    return inp['a'] + inp['b']\n\n"
        "def bad(inp):\n    return 0 if inp['a'] % 2 else inp['a'] + inp['b']\n"
    )
    data = tmp_path / "arith.jsonl"
    data.write_text(
        "\n".join(json.dumps({"input": {"a": i, "b": i}, "expected": 2 * i}) for i in range(40))
    )
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "cli.db")
    runner = CliRunner()
    r1 = runner.invoke(app, ["run", str(data), "apps.calc:good", "--db", db, "--out", "base.json"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(app, ["run", str(data), "apps.calc:bad", "--db", db, "--out", "cand.json"])
    assert r2.exit_code == 0, r2.output
    cmp_ = runner.invoke(app, ["compare", "base.json", "cand.json", "--db", db])
    assert "95% CI" in cmp_.output and "worse" in cmp_.output
    g = runner.invoke(
        app,
        ["gate", "base.json", "cand.json", "-s", "exact_match", "--db", db, "--report", "pr.md"],
    )
    assert g.exit_code == 1 and "failed" in (tmp_path / "pr.md").read_text()
    same = runner.invoke(app, ["gate", "base.json", "base.json", "-s", "exact_match", "--db", db])
    assert same.exit_code == 0
    listing = runner.invoke(app, ["dataset", "list", "--db", db])
    assert "arith@" in listing.output
