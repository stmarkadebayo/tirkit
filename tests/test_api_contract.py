"""API contract: every operation in the OpenAPI document is called against a seeded server and
its response is validated against the document's response schema (the schema that is checked in
as ``docs/api/openapi.json`` and generates the web UI's TypeScript types).

A new route fails ``test_every_operation_has_a_contract_call`` until a call is added to ``CALLS``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_server import basic, make_settings, new_key, setup_admin  # noqa: E402
from tirkit import jobs  # noqa: E402
from tirkit.server.app import create_app  # noqa: E402
from tirkit.tracing import Span  # noqa: E402

pytestmark = pytest.mark.server


@dataclass
class Seed:
    client: TestClient
    pid: str
    key: dict[str, str]
    ids: dict[str, str] = field(default_factory=dict)
    run: dict[str, Any] = field(default_factory=dict)


def _seed(c: TestClient, webhook_url: str) -> Seed:
    from tirkit.datasets import Dataset, Example
    from tirkit.runner import run
    from tirkit.scorers import ExactMatch

    pid, _ = setup_admin(c)
    pk, sk = new_key(c, pid)
    key = basic(pk, sk)
    s = Seed(c, pid, key)

    # Traces: a session with two traces, one with an error and a child span.
    now = time.time()
    spans = []
    for i in range(4):
        root = Span(
            name="agent",
            input={"q": f"question {i}"},
            output=f"answer {i}",
            start=now - 60 + i,
            session_id="sess-1",
            user_id="user-1",
            release="v1",
            tags=["demo"],
        )
        root.end = root.start + 0.5
        child = Span(
            name="llm-call",
            trace_id=root.trace_id,
            parent_id=root.span_id,
            kind="llm",
            model="gpt-4o-mini",
            input=[{"role": "user", "content": f"question {i}"}],
            output=f"answer {i}",
            start=root.start + 0.1,
        )
        child.end = child.start + 0.3
        child.set_usage(input_tokens=10, output_tokens=5)
        if i == 3:
            child.record_error("boom")
        spans += [root.to_dict(), child.to_dict()]
    r = c.post("/api/v1/ingest", json={"spans": spans}, headers=key)
    assert r.status_code == 200, r.text
    s.ids["trace_id"] = spans[0]["trace_id"]

    # Media.
    import hashlib

    data = b"\x89PNG fake image"
    s.ids["sha256"] = hashlib.sha256(data).hexdigest()
    s.ids["media_bytes"] = data.decode("latin-1")

    # Datasets and experiments.
    ds = Dataset("qa", [Example({"q": "a"}, "A"), Example({"q": "b"}, "B")])
    c.put(f"/api/projects/{pid}/datasets/qa", json={"items": [vars(e) for e in ds.examples]})
    base = run(ds, lambda inp: inp["q"].upper(), [ExactMatch()], name="base")
    cand = run(ds, lambda inp: "A", [ExactMatch()], name="cand")
    for res in (base, cand):
        r = c.post(f"/api/projects/{pid}/experiments", json=res.to_json(), headers=key)
        assert r.status_code == 201, r.text
    s.run = cand.to_json()
    s.ids["experiment_id"], s.ids["experiment_b"] = base.experiment_id, cand.experiment_id

    # Prompts.
    r = c.post(
        f"/api/projects/{pid}/prompts/greeter/versions",
        json={"template": [{"role": "user", "content": "Say {{q}}"}], "labels": ["production"]},
    )
    assert r.status_code == 201, r.text

    # Annotation: a config, a queue with items, one completed label.
    c.put(
        f"/api/projects/{pid}/score-configs/correct",
        json={"data_type": "numeric", "min_value": 1, "max_value": 5},
    )
    s.ids["queue_id"] = c.post(
        f"/api/projects/{pid}/queues", json={"name": "Week 1", "score_names": ["correct"]}
    ).json()["queue_id"]
    c.post(
        f"/api/projects/{pid}/queues/{s.ids['queue_id']}/items", json={"filter": {}, "limit": 10}
    )
    for score in (3, 4):
        item = c.get(f"/api/projects/{pid}/queues/{s.ids['queue_id']}/next").json()["item"]
        c.post(
            f"/api/projects/{pid}/queues/{s.ids['queue_id']}/items/{item['item_id']}/complete",
            json={"scores": {"correct": score}},
        )

    # Online evaluation, alerts, a judge report and a UI experiment (run by the worker).
    s.ids["evaluator_id"] = c.post(
        f"/api/projects/{pid}/evaluators",
        json={"name": "json", "scorer": {"spec": "json_valid"}, "sampling_rate": 1},
    ).json()["evaluator_id"]
    s.ids["alert_id"] = c.post(
        f"/api/projects/{pid}/alerts",
        json={
            "name": "errors",
            "rule": {"metric": "error_rate", "op": ">", "threshold": 0.01},
            "channel": {"type": "webhook", "url": webhook_url},
        },
    ).json()["alert_id"]
    c.post(
        f"/api/projects/{pid}/judge-reports",
        json={
            "queue_id": s.ids["queue_id"],
            "label": "correct",
            "model": "fake-judge",
            "rubric_text": "Score 1-5.",
        },
    )
    s.ids["job_id"] = c.post(
        f"/api/projects/{pid}/experiments/run",
        json={"dataset": "qa", "template": "{{q}}", "kind": "text", "model": "fake-small"},
    ).json()["job_id"]
    while jobs.run_one(c.app.state.store, "contract"):
        pass
    return s


# (method, OpenAPI path, request options, expected status). Path parameters are filled from the
# seed (project_id, trace_id, queue_id, ...) unless given under "path". Deletes come last.
CALLS: list[tuple[str, str, dict[str, Any], int]] = [
    ("GET", "/api/health", {}, 200),
    ("GET", "/api/setup", {}, 200),
    ("GET", "/api/auth/me", {}, 200),
    ("GET", "/api/v1/project", {"key": True}, 200),
    ("POST", "/api/projects", {"json": {"name": "Second"}}, 201),
    ("GET", "/api/projects/{project_id}", {}, 200),
    ("PUT", "/api/projects/{project_id}/settings", {"json": {"retention_days": 30}}, 200),
    ("GET", "/api/projects/{project_id}/members", {}, 200),
    (
        "POST",
        "/api/projects/{project_id}/members",
        {"json": {"email": "m@example.com", "password": "another long password", "role": "viewer"}},
        201,
    ),
    ("GET", "/api/projects/{project_id}/keys", {}, 200),
    ("POST", "/api/projects/{project_id}/keys", {"json": {"name": "ci"}}, 201),
    (
        "POST",
        "/api/projects/{project_id}/keys/{public_key}/rotate",
        {"json": {"overlap_s": 60}},
        201,
    ),
    ("GET", "/api/projects/{project_id}/audit", {"params": {"limit": 5}}, 200),
    ("GET", "/api/admin/audit", {"params": {"action": "key."}}, 200),
    ("GET", "/api/projects/{project_id}/prices", {}, 200),
    (
        "PUT",
        "/api/projects/{project_id}/prices/{model}",
        {"path": {"model": "my-model"}, "json": {"input_tokens": 0.000001}},
        200,
    ),
    ("POST", "/api/v1/ingest", {"key": True, "json": {"spans": []}}, 200),
    ("POST", "/v1/traces", {"key": True, "json_otlp": True}, 200),
    ("PUT", "/api/v1/media/{sha256}", {"key": True, "media": True}, 201),
    ("GET", "/api/v1/media/{sha256}", {"key": True}, 200),
    ("GET", "/api/projects/{project_id}/media/{sha256}", {}, 200),
    (
        "POST",
        "/api/v1/scores",
        {"key": True, "json": {"name": "thumbs", "value": 1, "trace_id": "{trace_id}"}},
        201,
    ),
    (
        "POST",
        "/api/projects/{project_id}/scores",
        {"json": {"name": "review", "value": 0.5, "trace_id": "{trace_id}", "comment": "ok"}},
        201,
    ),
    ("GET", "/api/projects/{project_id}/traces", {}, 200),
    ("GET", "/api/projects/{project_id}/traces/{trace_id}", {}, 200),
    ("GET", "/api/projects/{project_id}/spans", {}, 200),
    ("GET", "/api/projects/{project_id}/sessions", {}, 200),
    (
        "GET",
        "/api/projects/{project_id}/sessions/{session_id}",
        {"path": {"session_id": "sess-1"}},
        200,
    ),
    ("GET", "/api/projects/{project_id}/users", {}, 200),
    ("GET", "/api/projects/{project_id}/filters", {}, 200),
    ("GET", "/api/projects/{project_id}/metrics", {"params": {"group_by": "model"}}, 200),
    ("GET", "/api/projects/{project_id}/export/traces.jsonl", {}, 200),
    ("GET", "/api/projects/{project_id}/datasets", {}, 200),
    ("GET", "/api/projects/{project_id}/datasets/{name}", {"path": {"name": "qa"}}, 200),
    (
        "POST",
        "/api/projects/{project_id}/datasets/from-traces",
        {"json": {"name": "mined", "filter": {"name": "agent"}}},
        201,
    ),
    (
        "PUT",
        "/api/projects/{project_id}/datasets/{name}",
        {"path": {"name": "edited"}, "json": {"items": [{"input": "x"}]}},
        200,
    ),
    (
        "POST",
        "/api/projects/{project_id}/datasets/{name}/upload",
        {"path": {"name": "uploaded"}, "content": '{"input": {"q": "x"}, "expected": "X"}\n'},
        201,
    ),
    ("POST", "/api/projects/{project_id}/experiments", {"run": True}, 201),
    ("GET", "/api/projects/{project_id}/experiments", {}, 200),
    ("GET", "/api/projects/{project_id}/experiments/{experiment_id}", {}, 200),
    (
        "GET",
        "/api/projects/{project_id}/compare",
        {"params": {"baseline": "{experiment_id}", "candidate": "{experiment_b}"}},
        200,
    ),
    ("GET", "/api/projects/{project_id}/prompts", {}, 200),
    (
        "GET",
        "/api/projects/{project_id}/prompts/{name}/versions",
        {"path": {"name": "greeter"}},
        200,
    ),
    (
        "POST",
        "/api/projects/{project_id}/prompts/{name}/versions",
        {
            "path": {"name": "greeter"},
            "json": {"template": "Hello {{q}}", "kind": "text", "message": "plain"},
        },
        201,
    ),
    (
        "PUT",
        "/api/projects/{project_id}/prompts/{name}/labels/{label}",
        {"path": {"name": "greeter", "label": "staging"}, "json": {"version": 2}},
        200,
    ),
    (
        "GET",
        "/api/v1/prompts/{name}",
        {"key": True, "path": {"name": "greeter"}, "params": {"label": "production"}},
        200,
    ),
    ("GET", "/api/projects/{project_id}/playground/models", {}, 200),
    (
        "POST",
        "/api/projects/{project_id}/playground/run",
        {
            "json": {
                "template": "hi {{who}}",
                "kind": "text",
                "variables": {"who": "x"},
                "model": "fake-small",
            }
        },
        200,
    ),
    (
        "POST",
        "/api/projects/{project_id}/experiments/run",
        {"json": {"dataset": "qa", "template": "{{q}}", "kind": "text", "model": "fake-small"}},
        202,
    ),
    ("GET", "/api/projects/{project_id}/jobs", {}, 200),
    ("GET", "/api/projects/{project_id}/jobs/{job_id}", {}, 200),
    ("GET", "/api/projects/{project_id}/score-configs", {}, 200),
    (
        "PUT",
        "/api/projects/{project_id}/score-configs/{name}",
        {"path": {"name": "pass"}, "json": {"data_type": "boolean"}},
        200,
    ),
    ("GET", "/api/projects/{project_id}/queues", {}, 200),
    (
        "POST",
        "/api/projects/{project_id}/queues",
        {"json": {"name": "Week 2", "score_names": ["pass"]}},
        201,
    ),
    (
        "POST",
        "/api/projects/{project_id}/queues/{queue_id}/items",
        {"json": {"trace_ids": ["{trace_id}"]}},
        200,
    ),
    ("GET", "/api/projects/{project_id}/queues/{queue_id}/next", {}, 200),
    (
        "POST",
        "/api/projects/{project_id}/queues/{queue_id}/items/{item_id}/complete",
        {"json": {"scores": {"correct": 5}}},
        200,
    ),
    ("POST", "/api/projects/{project_id}/queues/{queue_id}/items/{item_id}/skip", {}, 200),
    ("GET", "/api/projects/{project_id}/queues/{queue_id}/labels.jsonl", {}, 200),
    ("GET", "/api/projects/{project_id}/evaluators", {}, 200),
    (
        "POST",
        "/api/projects/{project_id}/evaluators",
        {"json": {"name": "exact", "scorer": {"spec": "json_valid"}, "sampling_rate": 0.5}},
        201,
    ),
    (
        "PATCH",
        "/api/projects/{project_id}/evaluators/{evaluator_id}",
        {"json": {"active": False}},
        200,
    ),
    ("GET", "/api/projects/{project_id}/alerts", {}, 200),
    (
        "POST",
        "/api/projects/{project_id}/alerts",
        {
            "json": {
                "name": "slow",
                "rule": {"metric": "p95_latency_ms", "op": ">", "threshold": 5000},
                "channel": {"type": "email", "to": "ops@example.com"},
            }
        },
        201,
    ),
    ("POST", "/api/projects/{project_id}/alerts/{alert_id}/check", {}, 200),
    ("GET", "/api/projects/{project_id}/alerts", {}, 200),  # again, now with an event
    ("GET", "/api/projects/{project_id}/judge-reports", {}, 200),
    (
        "POST",
        "/api/projects/{project_id}/judge-reports",
        {
            "json": {
                "queue_id": "{queue_id}",
                "label": "correct",
                "model": "fake-judge",
                "rubric_text": "1-5",
            }
        },
        202,
    ),
    ("DELETE", "/api/projects/{project_id}/evaluators/{evaluator_id}", {}, 200),
    ("DELETE", "/api/projects/{project_id}/alerts/{alert_id}", {}, 200),
    ("DELETE", "/api/projects/{project_id}/keys/{public_key}", {}, 200),
    ("DELETE", "/api/projects/{project_id}/members/{user_id}", {}, 200),
    ("POST", "/api/auth/logout", {}, 200),
    (
        "POST",
        "/api/auth/login",
        {"json": {"email": "admin@example.com", "password": "correct horse battery"}},
        200,
    ),
    (
        "POST",
        "/api/setup",
        {"json": {"email": "x@example.com", "password": "correct horse battery"}},
        409,
    ),
]


@pytest.fixture
def seeded(tmp_path, fake_llm, webhook) -> Iterator[Seed]:
    with TestClient(create_app(make_settings(tmp_path))) as c:
        yield _seed(c, webhook[0])


def _fill(text: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


def _deep_fill(obj: Any, values: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return _fill(obj, values)
    if isinstance(obj, dict):
        return {k: _deep_fill(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_fill(v, values) for v in obj]
    return obj


def _call(s: Seed, method: str, template: str, opts: dict[str, Any]) -> Any:
    values = {**s.ids, **opts.get("path", {})}
    req: dict[str, Any] = {}
    headers: dict[str, str] = {}
    if opts.get("key"):
        headers.update(s.key)
    if "json" in opts:
        req["json"] = _deep_fill(opts["json"], values)
    if opts.get("run"):
        req["json"] = {**s.run, "experiment_id": "contract-run"}
    if "params" in opts:
        req["params"] = _deep_fill(opts["params"], values)
    if "content" in opts:
        req["content"] = opts["content"]
    if opts.get("media"):
        req["content"] = s.ids["media_bytes"].encode("latin-1")
        headers["content-type"] = "image/png"
    if opts.get("json_otlp"):
        req["content"] = json.dumps({"resourceSpans": []})
        headers["content-type"] = "application/json"
    return s.client.request(method, _fill(template, values), headers=headers, **req)


def _operations(schema: dict[str, Any]) -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, ops in schema["paths"].items() for method in ops}


def test_every_operation_has_a_contract_call(tmp_path) -> None:
    schema = create_app(make_settings(tmp_path)).openapi()
    called = {(m, p) for m, p, _, _ in CALLS}
    assert _operations(schema) - called == set(), "operations with no contract call"
    assert called - _operations(schema) == set(), "contract calls to unknown operations"


def _response_schema(schema: dict[str, Any], method: str, path: str, status: int) -> Any:
    responses = schema["paths"][path][method.lower()]["responses"]
    content = responses.get(str(status), {}).get("content", {})
    return content.get("application/json", {}).get("schema")


def test_responses_match_the_openapi_schema(seeded) -> None:
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    s = seeded
    s.ids["project_id"] = s.pid
    schema = s.client.app.openapi()
    registry = Registry().with_resource("urn:openapi", DRAFT202012.create_resource(schema))
    dump = os.environ.get("TIRKIT_DUMP_API")
    dumped: dict[str, Any] = {}
    failures = []
    for method, template, opts, status in CALLS:
        if "{item_id}" in template:
            nxt = s.client.get(f"/api/projects/{s.pid}/queues/{s.ids['queue_id']}/next").json()
            s.ids["item_id"] = nxt["item"]["item_id"]
        r = _call(s, method, template, opts)
        label = f"{method} {template}"
        if r.status_code != status:
            failures.append(f"{label}: {r.status_code} {r.text[:300]}")
            continue
        if method == "POST" and template == "/api/projects/{project_id}/keys":
            s.ids["public_key"] = r.json()["public_key"]
        if method == "POST" and template == "/api/projects/{project_id}/members":
            s.ids["user_id"] = r.json()["user_id"]
        if status >= 400 or "application/json" not in r.headers.get("content-type", ""):
            continue
        body = r.json()
        dumped[label] = body
        expected = _response_schema(schema, method, template, status)
        if expected is None:
            failures.append(f"{label}: no JSON response schema for {status}")
            continue
        validator = Draft202012Validator(_rebase(expected), registry=registry, format_checker=None)
        errors = sorted(validator.iter_errors(body), key=lambda e: list(e.path))
        failures += [f"{label}: {list(e.path)}: {e.message[:200]}" for e in errors[:5]]
    if dump:
        with open(dump, "w") as f:
            json.dump(dumped, f, indent=1, default=str)
    assert not failures, "\n".join(failures)


def _rebase(node: Any) -> Any:
    """Point local ``#/components/...`` references at the registered OpenAPI document."""
    if isinstance(node, dict):
        return {
            k: ("urn:openapi" + v if k == "$ref" and v.startswith("#/") else _rebase(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_rebase(v) for v in node]
    return node
