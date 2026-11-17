"""Server: setup, auth, CSRF, API keys, project isolation, ingestion, OTLP, media, scores."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tirkit.server.app import create_app  # noqa: E402
from tirkit.server.settings import Settings  # noqa: E402
from tirkit.tracing import Span  # noqa: E402

PASSWORD = "correct horse battery"


def make_settings(tmp_path: Any, db: str | None = None) -> Settings:
    s = Settings()
    s.database_url = db or str(tmp_path / "server.db")
    s.media_dir = tmp_path / "media"
    s.secure_cookies = False
    s.web_dir = None
    s.embedded_worker = False
    return s


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(tmp_path))) as c:
        yield c


def setup_admin(c: TestClient) -> tuple[str, str]:
    r = c.post("/api/setup", json={"email": "admin@example.com", "password": PASSWORD})
    assert r.status_code == 200, r.text
    me = c.get("/api/auth/me").json()
    c.headers["X-CSRF-Token"] = me["csrf"]
    return r.json()["project_id"], me["csrf"]


def new_key(c: TestClient, pid: str) -> tuple[str, str]:
    r = c.post(f"/api/projects/{pid}/keys", json={"name": "test"})
    assert r.status_code == 201, r.text
    return r.json()["public_key"], r.json()["secret_key"]


def basic(pk: str, sk: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()}


def test_setup_login_and_csrf(client) -> None:
    assert client.get("/api/setup").json() == {"needs_setup": True}
    pid, _ = setup_admin(client)
    assert client.get("/api/setup").json() == {"needs_setup": False}
    assert (
        client.post("/api/setup", json={"email": "x@y.z", "password": PASSWORD}).status_code == 409
    )
    me = client.get("/api/auth/me").json()
    assert me["projects"][0]["project_id"] == pid
    # A state-changing request without the CSRF header is refused.
    no_csrf = client.post("/api/projects", json={"name": "P2"}, headers={"X-CSRF-Token": ""})
    assert no_csrf.status_code == 403
    assert client.post("/api/projects", json={"name": "P2"}).status_code == 201
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401
    assert (
        client.post(
            "/api/auth/login", json={"email": "admin@example.com", "password": "nope-nope-nope"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login", json={"email": "ADMIN@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )


def test_ingest_read_and_project_isolation(client) -> None:
    pid, _ = setup_admin(client)
    other = client.post("/api/projects", json={"name": "Other"}).json()["project_id"]
    pk, sk = new_key(client, pid)
    s = Span(name="llm", kind="llm", model="gpt-4o-mini", input="hi", output="yo")
    s.end = s.start + 0.2
    s.set_usage(input_tokens=1000, output_tokens=100)
    r = client.post("/api/v1/ingest", json={"spans": [s.to_dict()]}, headers=basic(pk, sk))
    assert r.json() == {"accepted": 1}
    traces = client.get(f"/api/projects/{pid}/traces").json()
    assert traces["total"] == 1 and traces["items"][0]["cost_usd"] > 0
    detail = client.get(f"/api/projects/{pid}/traces/{s.trace_id}").json()
    assert detail["spans"][0]["output"] == "yo"
    # The key reads its own project but not another one.
    assert client.get(f"/api/projects/{pid}/traces", headers=basic(pk, sk)).status_code == 200
    assert client.get(f"/api/projects/{other}/traces", headers=basic(pk, sk)).status_code == 403
    assert client.get(f"/api/projects/{other}/traces").json()["total"] == 0
    assert (
        client.post("/api/v1/ingest", json={"spans": []}, headers=basic(pk, "wrong")).status_code
        == 401
    )


def test_non_member_cannot_see_project(client) -> None:
    pid, _ = setup_admin(client)
    client.post(
        f"/api/projects/{pid}/members",
        json={"email": "bob@example.com", "password": PASSWORD, "role": "viewer"},
    )
    other = client.post("/api/projects", json={"name": "Secret"}).json()["project_id"]
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": "bob@example.com", "password": PASSWORD})
    client.headers["X-CSRF-Token"] = client.get("/api/auth/me").json()["csrf"]
    assert client.get(f"/api/projects/{pid}/traces").status_code == 200
    assert client.get(f"/api/projects/{other}/traces").status_code == 404
    assert client.post(f"/api/projects/{pid}/keys", json={}).status_code == 403  # viewer
    assert (
        client.post(
            f"/api/projects/{pid}/scores", json={"trace_id": "t", "name": "x", "value": 1}
        ).status_code
        == 403
    )


def test_otlp_json_and_protobuf(client) -> None:
    pid, _ = setup_admin(client)
    pk, sk = new_key(client, pid)
    otlp = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "shop"}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "opentelemetry.instrumentation.openai"},
                        "spans": [
                            {
                                "traceId": "5b8efff798038103d269b633813fc60c",
                                "spanId": "eee19b7ec3c1b174",
                                "name": "chat gpt-4o",
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000001500000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.operation.name",
                                        "value": {"stringValue": "chat"},
                                    },
                                    {
                                        "key": "gen_ai.provider.name",
                                        "value": {"stringValue": "openai"},
                                    },
                                    {
                                        "key": "gen_ai.request.model",
                                        "value": {"stringValue": "gpt-4o"},
                                    },
                                    {
                                        "key": "gen_ai.usage.input_tokens",
                                        "value": {"intValue": "120"},
                                    },
                                    {
                                        "key": "gen_ai.usage.output_tokens",
                                        "value": {"intValue": "30"},
                                    },
                                    {
                                        "key": "gen_ai.usage.cache_read.input_tokens",
                                        "value": {"intValue": "100"},
                                    },
                                    {
                                        "key": "gen_ai.request.temperature",
                                        "value": {"doubleValue": 0.2},
                                    },
                                    {
                                        "key": "gen_ai.conversation.id",
                                        "value": {"stringValue": "conv-1"},
                                    },
                                    {
                                        "key": "gen_ai.input.messages",
                                        "value": {
                                            "arrayValue": {
                                                "values": [
                                                    {
                                                        "kvlistValue": {
                                                            "values": [
                                                                {
                                                                    "key": "role",
                                                                    "value": {
                                                                        "stringValue": "user"
                                                                    },
                                                                },
                                                                {
                                                                    "key": "parts",
                                                                    "value": {
                                                                        "arrayValue": {
                                                                            "values": [
                                                                                {
                                                                                    "kvlistValue": {
                                                                                        "values": [
                                                                                            {
                                                                                                "key": "type",
                                                                                                "value": {
                                                                                                    "stringValue": "text"
                                                                                                },
                                                                                            },
                                                                                            {
                                                                                                "key": "content",
                                                                                                "value": {
                                                                                                    "stringValue": "Weather in Paris?"
                                                                                                },
                                                                                            },
                                                                                        ]
                                                                                    }
                                                                                }
                                                                            ]
                                                                        }
                                                                    },
                                                                },
                                                            ]
                                                        }
                                                    }
                                                ]
                                            }
                                        },
                                    },
                                    {
                                        "key": "gen_ai.output.messages",
                                        "value": {
                                            "stringValue": '[{"role": "assistant", "parts": [{"type": "text", "content": "Rainy"}]}]'
                                        },
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    r = client.post(
        "/v1/traces",
        content=json.dumps(otlp),
        headers={**basic(pk, sk), "content-type": "application/json"},
    )
    assert r.status_code == 200, r.text
    spans = client.get(f"/api/projects/{pid}/spans").json()["items"]
    s = spans[0]
    assert s["kind"] == "llm" and s["model"] == "gpt-4o" and s["session_id"] == "conv-1"
    assert s["usage"] == {"input_tokens": 120, "output_tokens": 30, "cached_input_tokens": 100}
    assert s["input"]["messages"][0]["parts"][0]["content"] == "Weather in Paris?"
    assert s["output"][0]["parts"][0]["content"] == "Rainy"
    assert s["model_params"] == {"temperature": 0.2}
    assert s["attributes"]["service.name"] == "shop" and s["cost_usd"] > 0
    assert s["end"] - s["start"] == pytest.approx(1.5)

    from google.protobuf.json_format import ParseDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    proto_payload = json.loads(json.dumps(otlp))
    span = proto_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    span["traceId"] = base64.b64encode(bytes.fromhex("aa" * 16)).decode()
    span["spanId"] = base64.b64encode(bytes.fromhex("bb" * 8)).decode()
    msg = ParseDict(proto_payload, ExportTraceServiceRequest())
    r = client.post(
        "/v1/traces",
        content=msg.SerializeToString(),
        headers={**basic(pk, sk), "content-type": "application/x-protobuf"},
    )
    assert r.status_code == 200, r.text
    assert client.get(f"/api/projects/{pid}/traces/{'aa' * 16}").status_code == 200


def test_media_upload_and_serving(client) -> None:
    import hashlib

    pid, _ = setup_admin(client)
    pk, sk = new_key(client, pid)
    data = b"\x89PNG\r\n\x1a\n" + b"\1" * 100
    sha = hashlib.sha256(data).hexdigest()
    assert (
        client.put(f"/api/v1/media/{'0' * 64}", content=data, headers=basic(pk, sk)).status_code
        == 400
    )
    r = client.put(
        f"/api/v1/media/{sha}", content=data, headers={**basic(pk, sk), "content-type": "image/png"}
    )
    assert r.status_code == 201
    got = client.get(f"/api/projects/{pid}/media/{sha}")
    assert got.content == data and got.headers["content-type"] == "image/png"
    assert got.headers["content-security-policy"] == "sandbox"
    other = client.post("/api/projects", json={"name": "O"}).json()["project_id"]
    assert client.get(f"/api/projects/{other}/media/{sha}").status_code == 404


def test_ingest_redaction_setting(client) -> None:
    pid, _ = setup_admin(client)
    pk, sk = new_key(client, pid)
    client.put(
        f"/api/projects/{pid}/settings",
        json={"redact": True, "redact_patterns": [{"pattern": r"CUST-\d+", "label": "[CUSTOMER]"}]},
    )
    s = Span(name="a", input={"q": "mail me at x.y@example.com, I am CUST-1234"})
    client.post("/api/v1/ingest", json={"spans": [s.to_dict()]}, headers=basic(pk, sk))
    got = client.get(f"/api/projects/{pid}/spans").json()["items"][0]["input"]
    assert got == {"q": "mail me at [EMAIL], I am [CUSTOMER]"}


def test_metrics_sessions_users_and_datasets(client) -> None:
    pid, _ = setup_admin(client)
    pk, sk = new_key(client, pid)
    now = time.time()
    spans = []
    for i in range(20):
        s = Span(
            name="agent",
            start=now - 3600 + i * 60,
            input={"q": f"question number {i} about returns policy"},
            output="a",
            session_id=f"s{i % 3}",
            user_id=f"u{i % 2}",
            model="gpt-4o-mini",
            status="error" if i == 5 else "ok",
        )
        s.end = s.start + 0.1 * (i + 1)
        s.set_usage(input_tokens=10, output_tokens=5)
        spans.append(s.to_dict())
    client.post("/api/v1/ingest", json={"spans": spans}, headers=basic(pk, sk))
    m = client.get(
        f"/api/projects/{pid}/metrics", params={"since": now - 7200, "until": now, "buckets": 4}
    ).json()
    pts = m["series"]["all"]
    assert sum(p["spans"] for p in pts) == 20 and sum(p["errors"] for p in pts) == 1
    assert all("p99_ms" in p for p in pts if p.get("traces"))
    by_model = client.get(
        f"/api/projects/{pid}/metrics",
        params={"since": now - 7200, "until": now, "group_by": "model"},
    ).json()
    assert "gpt-4o-mini" in by_model["series"]
    assert len(client.get(f"/api/projects/{pid}/sessions").json()) == 3
    assert {u["user_id"] for u in client.get(f"/api/projects/{pid}/users").json()} == {"u0", "u1"}
    r = client.post(f"/api/projects/{pid}/datasets/from-traces", json={"name": "faq"})
    assert r.status_code == 201 and r.json()["n_items"] >= 1
    ds = client.get(f"/api/projects/{pid}/datasets/faq").json()
    assert ds["items"][0]["metadata"]["source_trace_id"]
    export = client.get(f"/api/projects/{pid}/export/traces.jsonl")
    assert len(export.text.strip().splitlines()) == 20


def test_sdk_server_mode_with_media_and_feedback(live_server) -> None:
    import httpx

    import tirkit

    url, app = live_server
    c = httpx.Client(base_url=url)
    pid = c.post("/api/setup", json={"email": "a@example.com", "password": PASSWORD}).json()[
        "project_id"
    ]
    c.headers["X-CSRF-Token"] = c.get("/api/auth/me").json()["csrf"]
    keys = c.post(f"/api/projects/{pid}/keys", json={}).json()
    client = tirkit.init(host=url, public_key=keys["public_key"], secret_key=keys["secret_key"])
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 4000
    with tirkit.span(
        "vision", kind="llm", model="gpt-4o", input={"image": png, "q": "what is it?"}
    ) as s:
        s.output = "a cat"
    tirkit.feedback(s.trace_id, 1, comment="great")
    assert tirkit.flush(10)
    client.shutdown()
    detail = c.get(f"/api/projects/{pid}/traces/{s.trace_id}").json()
    ref = detail["spans"][0]["input"]["image"]
    assert ref["type"] == "media" and ref["mime"] == "image/png"
    assert c.get(f"/api/projects/{pid}/media/{ref['sha256']}").content == png
    assert detail["scores"][0]["source"] == "user_feedback" and detail["scores"][0]["value"] == 1


@pytest.mark.server
def test_serves_built_ui_with_spa_fallback(tmp_path) -> None:
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html>tirkit ui</html>")
    (web / "assets" / "app.js").write_text("console.log(1)")
    s = make_settings(tmp_path)
    s.web_dir = web
    with TestClient(create_app(s)) as c:
        assert "tirkit ui" in c.get("/").text
        assert "tirkit ui" in c.get("/p/x/traces/abc").text  # client-side route
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/api/nope").status_code == 404
        assert c.get("/../../etc/passwd").status_code in (200, 404)  # never escapes web dir
        assert "root:" not in c.get("/..%2f..%2fetc/passwd").text
