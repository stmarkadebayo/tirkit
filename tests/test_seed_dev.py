"""The development seed (examples/seed_dev.py) fills every screen, deterministically and twice-safe.

The data is synthetic. These tests only check that the seed produces the shape of data the UI
needs; none of the numbers it produces are results.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_server import make_settings  # noqa: E402
from tirkit.server.app import create_app  # noqa: E402

SEED_PATH = Path(__file__).resolve().parents[1] / "examples" / "seed_dev.py"


def load_seed() -> ModuleType:
    spec = importlib.util.spec_from_file_location("seed_dev", SEED_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seed_dev"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def app_and_client(tmp_path: Any) -> Iterator[tuple[Any, TestClient]]:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        yield app, c


def run_seed(app: Any, client: TestClient, mod: ModuleType) -> dict[str, Any]:
    seeder = mod.Seeder(client, log=lambda *_: None)
    seeder.login()
    seeder.store = app.state.store.for_project(seeder.pid)
    return seeder.run()


@pytest.mark.server
def test_seed_fills_every_screen(app_and_client: tuple[Any, TestClient]) -> None:
    app, client = app_and_client
    mod = load_seed()
    counts = run_seed(app, client, mod)

    assert counts["traces"] >= 140
    assert counts["sessions"] >= 30 and counts["users"] >= 5
    assert counts["datasets"] >= 1 and counts["dataset_versions"] >= 2
    assert counts["experiments"] >= 2
    assert counts["labelled"] == mod.N_LABELLED
    assert counts["unlabelled"] == mod.N_UNLABELLED
    assert counts["judge_reports"] >= 1
    assert counts["prompts"] >= 2
    assert counts["evaluators"] >= 1 and counts["online_scored"] > 0
    assert counts["alerts"] >= 1 and counts["alerts_firing"] >= 1 and counts["alert_events"] >= 1


@pytest.mark.server
def test_seed_compare_has_intervals_regressions_and_improvements(
    app_and_client: tuple[Any, TestClient],
) -> None:
    app, client = app_and_client
    mod = load_seed()
    run_seed(app, client, mod)
    pid = client.get("/api/auth/me").json()["projects"][0]["project_id"]
    exps = {e["name"]: e for e in client.get(f"/api/projects/{pid}/experiments").json()}
    base = exps["baseline: support-answer@1"]["experiment_id"]
    better = exps["improved: support-answer@2"]["experiment_id"]
    worse = exps["terse: support-answer@3"]["experiment_id"]

    cmp = client.get(f"/api/projects/{pid}/compare", params={"baseline": base, "candidate": better})
    assert cmp.status_code == 200, cmp.text
    body = cmp.json()
    by = {c["scorer"]: c for c in body["comparisons"]}
    assert {"contains_expected", "wer", "judge:correctness"} <= set(by)
    for c in by.values():
        lo, hi = c["diff_ci"]
        assert lo <= c["diff"] <= hi and c["n"] > 30
    # Per-item diff with at least one regression and one improvement on the correctness judge.
    deltas = [
        i["candidate"]["scores"]["judge:correctness"] - i["baseline"]["scores"]["judge:correctness"]
        for i in body["items"]
    ]
    assert any(d > 0 for d in deltas) and any(d < 0 for d in deltas)
    assert by["judge:correctness"]["verdict"] == "better"

    # The terse run is confidently worse, so a gate on it fails.
    from tirkit.runner import gate
    from tirkit.types import RunResult

    store = app.state.store.for_project(pid)
    a, b = store.load_experiment(base), store.load_experiment(worse)
    assert isinstance(a, RunResult)
    assert not gate(a, b, "contains_expected", n_resamples=2000)
    assert gate(a, store.load_experiment(better), "contains_expected", n_resamples=2000)


@pytest.mark.server
def test_seed_judge_reliability_is_imperfect_but_populated(
    app_and_client: tuple[Any, TestClient],
) -> None:
    app, client = app_and_client
    run_seed(app, client, load_seed())
    pid = client.get("/api/auth/me").json()["projects"][0]["project_id"]
    reports = client.get(f"/api/projects/{pid}/judge-reports").json()["reports"]
    assert len(reports) == 2
    for r in reports:
        s = r["summary"]
        assert s["n_judged"] >= 55
        assert 0.1 < s["kappa"] < 0.95
        assert 0 < s["exact_agreement"] < 1
        assert s["n_pairs_decided"] > 0 and s["flip_rate"] is not None
        assert s["length_corr_judge"] is not None and s["length_corr_human"] is not None


@pytest.mark.server
def test_seed_prompts_have_versions_labels_and_linked_traces(
    app_and_client: tuple[Any, TestClient],
) -> None:
    app, client = app_and_client
    run_seed(app, client, load_seed())
    pid = client.get("/api/auth/me").json()["projects"][0]["project_id"]
    prompts = {p["name"]: p for p in client.get(f"/api/projects/{pid}/prompts").json()}
    assert prompts["support-answer"]["latest_version"] >= 2
    assert "production" in prompts["support-answer"]["labels"]
    spans = client.get(f"/api/projects/{pid}/spans", params={"limit": 500}).json()
    refs = {s.get("prompt_version") for s in spans["items"]}
    assert {"support-answer@1", "support-answer@2"} <= refs


@pytest.mark.server
def test_seed_twice_does_not_duplicate(app_and_client: tuple[Any, TestClient]) -> None:
    app, client = app_and_client
    mod = load_seed()
    first = run_seed(app, client, mod)
    second = run_seed(app, client, mod)
    assert second == first


def test_seed_is_deterministic_and_offline() -> None:
    mod = load_seed()
    a, b = mod.build_traffic(), mod.build_traffic()
    assert [(t.trace_id, t.question, t.output) for t in a] == [
        (t.trace_id, t.question, t.output) for t in b
    ]
    # Same input, same scripted answer and score, every time.
    q = mod.QUESTIONS[0][0]
    assert mod.system_output("baseline", q) == mod.system_output("baseline", q)
    judge = mod.ScriptedJudge(mod.RUBRIC_V2)
    from tirkit.types import Example

    ex = Example({"question": q})
    assert (
        judge.score(ex, mod.make_output(q, "correct")).value
        == judge.score(ex, mod.make_output(q, "correct")).value
    )
