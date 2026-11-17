"""Prompts, playground, UI-started experiments and the worker (M8)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_server import basic, make_settings, new_key, setup_admin  # noqa: E402
from tirkit.prompts import MissingVariable, Prompt, PromptStore, render  # noqa: E402
from tirkit.server.app import create_app  # noqa: E402


def test_render_and_variables() -> None:
    assert render("Hi {{ user.name }}, {{q}}", {"user": {"name": "Ada"}, "q": "ok"}) == "Hi Ada, ok"
    with pytest.raises(MissingVariable):
        render("{{missing}}", {})
    p = Prompt(
        "p",
        1,
        "chat",
        [{"role": "system", "content": "Be {{tone}}"}, {"role": "user", "content": "{{question}}"}],
    )
    assert p.variables == ["tone", "question"]
    assert p.compile(tone="brief", question="Why?")[1]["content"] == "Why?"


def test_versions_labels_and_local_get_prompt(tmp_path, sink) -> None:
    import tirkit
    from tirkit.tracing import span

    client = tirkit.init(path=tmp_path / "p.db")
    ps = PromptStore(client.store)
    v1 = ps.create_version("answer", "Answer: {{q}}", kind="text", labels=["production"])
    v2 = ps.create_version("answer", "Answer briefly: {{q}}", kind="text")
    assert (v1, v2) == (1, 2)
    assert tirkit.get_prompt("answer", cache_ttl_s=0).version == 1  # production
    assert tirkit.get_prompt("answer", label="latest", cache_ttl_s=0).version == 2
    ps.set_label("answer", "production", 2)
    p = tirkit.get_prompt("answer", cache_ttl_s=0)
    assert p.version == 2 and p.compile(q="x") == "Answer briefly: x"
    tirkit.tracing.set_default_sink(sink)
    with p.use():
        with span("call", kind="llm"):
            pass
    assert sink.spans[-1].prompt_version == "answer@2"
    with pytest.raises(ValueError):
        ps.create_version("bad", ["not a message"], kind="chat")
    client.shutdown()


@pytest.fixture
def server(tmp_path, fake_llm):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        pid, _ = setup_admin(c)
        yield c, pid


def test_prompt_api_and_sdk_fetch(server) -> None:
    c, pid = server
    pk, sk = new_key(c, pid)
    r = c.post(
        f"/api/projects/{pid}/prompts/support/versions",
        json={
            "kind": "chat",
            "template": [{"role": "user", "content": "Q: {{question}}"}],
            "config": {"model": "fake-small"},
            "labels": ["production"],
            "message": "first",
        },
    )
    assert r.json() == {"name": "support", "version": 1}
    c.post(
        f"/api/projects/{pid}/prompts/support/versions",
        json={"kind": "chat", "template": [{"role": "user", "content": "Question: {{question}}"}]},
    )
    versions = c.get(f"/api/projects/{pid}/prompts/support/versions").json()["versions"]
    assert [v["version"] for v in versions] == [2, 1] and versions[1]["labels"] == ["production"]
    assert versions[0]["variables"] == ["question"]
    got = c.get("/api/v1/prompts/support", headers=basic(pk, sk)).json()
    assert got["version"] == 1 and got["config"]["model"] == "fake-small"
    c.put(f"/api/projects/{pid}/prompts/support/labels/production", json={"version": 2})
    assert c.get("/api/v1/prompts/support", headers=basic(pk, sk)).json()["version"] == 2
    assert (
        c.get("/api/v1/prompts/support", params={"version": 1}, headers=basic(pk, sk)).json()[
            "version"
        ]
        == 1
    )
    assert c.get("/api/v1/prompts/nope", headers=basic(pk, sk)).status_code == 404


def test_playground_runs_model_and_traces_it(server) -> None:
    c, pid = server
    models = c.get(f"/api/projects/{pid}/playground/models").json()
    assert "fake-small" in models["models"]
    r = c.post(
        f"/api/projects/{pid}/playground/run",
        json={
            "kind": "chat",
            "template": [{"role": "user", "content": "hello {{name}}"}],
            "variables": {"name": "tirkit"},
            "model": "fake-small",
            "params": {"temperature": 0},
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["output"] == "HELLO TIRKIT" and out["usage"]["output_tokens"] == 5
    trace = c.get(f"/api/projects/{pid}/traces/{out['trace_id']}").json()
    assert trace["spans"][0]["name"] == "playground" and trace["spans"][0]["model_params"] == {
        "temperature": 0
    }
    missing = c.post(
        f"/api/projects/{pid}/playground/run",
        json={"kind": "text", "template": "{{x}}", "model": "fake-small"},
    )
    assert missing.status_code == 400


def test_ui_experiment_job_runs_prompt_over_dataset(server) -> None:
    from tirkit import jobs

    c, pid = server
    items = [
        {"input": {"question": q}, "expected": q.upper()} for q in ["a b", "c d", "e f", "g h"]
    ]
    c.put(f"/api/projects/{pid}/datasets/qa", json={"items": items})
    c.post(
        f"/api/projects/{pid}/prompts/echo/versions",
        json={"kind": "chat", "template": [{"role": "user", "content": "{{question}}"}]},
    )
    r = c.post(
        f"/api/projects/{pid}/experiments/run",
        json={
            "dataset": "qa",
            "prompt": {"name": "echo", "version": 1},
            "model": "fake-small",
            "scorers": ["exact_match", "judge:correctness"],
            "judge_model": "fake-judge",
        },
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    store = c.app.state.store
    assert jobs.run_one(store, "test-worker") is True
    job = c.get(f"/api/projects/{pid}/jobs/{job_id}").json()
    assert job["status"] == "done", job
    exp = c.get(f"/api/projects/{pid}/experiments/{job['result']['experiment_id']}").json()
    assert exp["summary"]["exact_match"]["mean"] == 1.0
    assert exp["summary"]["judge:correctness"]["mean"] == 4.0
    assert exp["config"]["prompt_version"] == "echo@1"
    spans = c.get(f"/api/projects/{pid}/spans", params={"name": "prompt:echo"}).json()["items"]
    assert spans and spans[0]["prompt_version"] == "echo@1"


def test_failed_job_records_error(server) -> None:
    from tirkit import jobs

    c, pid = server
    store = c.app.state.store.for_project(pid)
    job_id = jobs.enqueue(store, "experiment", {"dataset": "missing"})
    jobs.run_one(c.app.state.store, "w")
    job = jobs.get_job(store, job_id)
    assert job["status"] == "failed" and "missing" in job["error"]
