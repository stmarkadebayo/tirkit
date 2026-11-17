"""Annotation queues, score configs and the server-side evaluation loop (M7)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_server import basic, make_settings, new_key, setup_admin  # noqa: E402
from tirkit.server.app import create_app  # noqa: E402
from tirkit.tracing import Span  # noqa: E402


def test_score_configs_queue_labelling_and_export(tmp_path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as c:
        pid, _ = setup_admin(c)
        pk, sk = new_key(c, pid)
        spans = []
        for i in range(25):
            s = Span(name="agent", input={"q": f"question {i}"}, output=f"answer {i}")
            s.end = s.start + 0.1
            spans.append(s.to_dict())
        c.post("/api/v1/ingest", json={"spans": spans}, headers=basic(pk, sk))
        assert (
            c.put(
                f"/api/projects/{pid}/score-configs/correct",
                json={"data_type": "numeric", "min_value": 1, "max_value": 5},
            ).status_code
            == 200
        )
        assert (
            c.put(
                f"/api/projects/{pid}/score-configs/pass", json={"data_type": "boolean"}
            ).status_code
            == 200
        )
        bad = c.put(f"/api/projects/{pid}/score-configs/x", json={"data_type": "categorical"})
        assert bad.status_code == 400
        qid = c.post(
            f"/api/projects/{pid}/queues",
            json={"name": "Week 1", "score_names": ["correct", "pass"]},
        ).json()["queue_id"]
        assert c.post(
            f"/api/projects/{pid}/queues/{qid}/items", json={"filter": {}, "limit": 100}
        ).json() == {"added": 25}
        assert c.post(
            f"/api/projects/{pid}/queues/{qid}/items", json={"filter": {}, "limit": 100}
        ).json() == {"added": 0}
        for n in range(20):  # a reviewer labels 20 items
            nxt = c.get(f"/api/projects/{pid}/queues/{qid}/next").json()
            item = nxt["item"]
            assert nxt["trace"]["input"]["q"].startswith("question")
            if n == 0:
                out_of_range = c.post(
                    f"/api/projects/{pid}/queues/{qid}/items/{item['item_id']}/complete",
                    json={"scores": {"correct": 9, "pass": True}},
                )
                assert out_of_range.status_code == 400
            r = c.post(
                f"/api/projects/{pid}/queues/{qid}/items/{item['item_id']}/complete",
                json={"scores": {"correct": 1 + n % 5, "pass": n % 2 == 0}, "comment": "ok"},
            )
            assert r.status_code == 200, r.text
        q = c.get(f"/api/projects/{pid}/queues").json()[0]
        assert (q["n_done"], q["n_items"]) == (20, 25)
        export = c.get(f"/api/projects/{pid}/queues/{qid}/labels.jsonl").text
        labels = [line for line in export.splitlines() if line]
        assert len(labels) == 20
        import json

        first = json.loads(labels[0])
        assert (
            set(first["labels"]) == {"correct", "pass"} and first["labeller"] == "admin@example.com"
        )
        scores = c.get(f"/api/projects/{pid}/traces/{first['trace_id']}").json()["scores"]
        assert {s["source"] for s in scores} == {"human"}


def test_sdk_run_lands_in_server_and_compares(live_server) -> None:
    import httpx

    import tirkit
    from tirkit.datasets import Dataset, Example
    from tirkit.runner import run
    from tirkit.scorers import ExactMatch

    url, _ = live_server
    c = httpx.Client(base_url=url)
    pid = c.post(
        "/api/setup", json={"email": "a@example.com", "password": "correct horse battery"}
    ).json()["project_id"]
    c.headers["X-CSRF-Token"] = c.get("/api/auth/me").json()["csrf"]
    keys = c.post(f"/api/projects/{pid}/keys", json={}).json()
    client = tirkit.init(host=url, public_key=keys["public_key"], secret_key=keys["secret_key"])
    ds = Dataset("arith", [Example({"a": i}, expected=2 * i) for i in range(30)])
    client.store.save_dataset(ds)
    loaded = client.store.load_dataset("arith")
    assert loaded.version == ds.version
    base = run(loaded, lambda x: 2 * x["a"], [ExactMatch()], name="good", store=client.store)
    cand = run(
        loaded,
        lambda x: 2 * x["a"] if x["a"] % 3 else 0,
        [ExactMatch()],
        name="bad",
        store=client.store,
    )
    client.shutdown()
    exps = c.get(f"/api/projects/{pid}/experiments").json()
    assert {e["name"] for e in exps} == {"good", "bad"}
    cmp_ = c.get(
        f"/api/projects/{pid}/compare",
        params={"baseline": base.experiment_id, "candidate": cand.experiment_id},
    ).json()
    assert cmp_["comparisons"][0]["verdict"] == "worse"
    assert len(cmp_["items"]) == 30 and cmp_["items"][0]["candidate"]["trace_id"]
    detail = c.get(f"/api/projects/{pid}/experiments/{base.experiment_id}").json()
    assert detail["summary"]["exact_match"]["mean"] == 1.0
