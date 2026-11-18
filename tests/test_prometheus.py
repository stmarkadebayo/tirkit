"""Prometheus ``/metrics`` (Phase F): ingest accepted/rejected by reason, ingest latency, buffer
depth, database pool, job queue depth and age, judge spend. No per-trace labels."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("prometheus_client")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_server import basic, make_settings, new_key, setup_admin  # noqa: E402
from tirkit.server.app import create_app  # noqa: E402
from tirkit.server.prometheus import set_buffer_depth_source  # noqa: E402
from tirkit.tracing import Span  # noqa: E402

pytestmark = pytest.mark.server

ALLOWED_LABELS = {"route", "outcome", "reason", "status", "state", "le"}


def parse(text: str) -> dict[str, float]:
    """``{'name{labels}': value}`` for every sample line."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.rpartition(" ")
        out[key] = float(value)
    return out


def sample(metrics: dict[str, float], name: str, **labels: str) -> float | None:
    for key, value in metrics.items():
        if not key.startswith(name):
            continue
        rest = key[len(name) :]
        if rest and not rest.startswith("{"):
            continue
        got = dict(re.findall(r'(\w+)="([^"]*)"', rest))
        if got == labels:
            return value
    return None


def scrape(c: TestClient, **headers: str) -> dict[str, float]:
    r = c.get("/metrics", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    return parse(r.text)


@pytest.fixture
def keyed(tmp_path) -> Any:
    with TestClient(create_app(make_settings(tmp_path))) as c:
        pid, _ = setup_admin(c)
        pk, sk = new_key(c, pid)
        yield c, basic(pk, sk), pid


def spans_payload(n: int) -> dict[str, Any]:
    now = time.time()
    root = Span(name="root", start=now, end=now + 1)
    rest = [
        Span(name=f"child{i}", trace_id=root.trace_id, parent_id=root.span_id, start=now, end=now)
        for i in range(n - 1)
    ]
    return {"spans": [s.to_dict() for s in [root, *rest]]}


def test_ingest_accepted_and_rejected_by_reason(keyed) -> None:
    c, key, _ = keyed
    assert c.post("/api/v1/ingest", json=spans_payload(3), headers=key).status_code == 200
    assert c.post("/api/v1/ingest", json=spans_payload(2), headers=key).status_code == 200
    bad = c.post("/api/v1/ingest", content=b"{not json", headers=key)
    assert bad.status_code == 400
    assert c.post("/api/v1/ingest", json=spans_payload(1)).status_code == 401
    big = b'{"spans": []}' + b" " * (20 * 1024 * 1024 + 1)
    assert c.post("/api/v1/ingest", content=big, headers=key).status_code == 413
    otlp = c.post(
        "/v1/traces",
        json={"resourceSpans": []},
        headers={**key, "content-type": "application/json"},
    )
    assert otlp.status_code == 200

    m = scrape(c)
    req = "tirkit_ingest_requests_total"
    assert sample(m, req, route="spans", outcome="accepted", reason="ok") == 2
    assert sample(m, req, route="spans", outcome="rejected", reason="invalid") == 1
    assert sample(m, req, route="spans", outcome="rejected", reason="auth") == 1
    assert sample(m, req, route="spans", outcome="rejected", reason="too_large") == 1
    assert sample(m, req, route="otlp", outcome="accepted", reason="ok") == 1
    assert sample(m, "tirkit_ingest_spans_total", route="spans") == 5
    assert sample(m, "tirkit_ingest_request_duration_seconds_count", route="spans") == 5
    assert sample(m, "tirkit_ingest_request_duration_seconds_count", route="otlp") == 1


def test_media_and_scores_are_counted(keyed) -> None:
    import hashlib

    c, key, _ = keyed
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    sha = hashlib.sha256(data).hexdigest()
    assert c.put(f"/api/v1/media/{sha}", content=data, headers=key).status_code == 201
    assert c.put(f"/api/v1/media/{'0' * 64}", content=data, headers=key).status_code == 400
    m = scrape(c)
    req = "tirkit_ingest_requests_total"
    assert sample(m, req, route="media", outcome="accepted", reason="ok") == 1
    assert sample(m, req, route="media", outcome="rejected", reason="invalid") == 1


def test_no_per_trace_labels(keyed) -> None:
    c, key, pid = keyed
    payload = spans_payload(2)
    c.post("/api/v1/ingest", json=payload, headers=key)
    text = c.get("/metrics").text
    trace_id = payload["spans"][0]["trace_id"]
    assert trace_id not in text
    assert pid not in text
    labels = set(re.findall(r'(\w+)="', text))
    assert labels <= ALLOWED_LABELS, labels - ALLOWED_LABELS


def test_job_queue_depth_and_age(keyed) -> None:
    from tirkit import jobs

    c, _, pid = keyed
    store = c.app.state.store.for_project(pid)  # type: ignore[attr-defined]
    jobs.enqueue(store, "noop", {})
    jobs.enqueue(store, "noop", {})
    jobs.enqueue(store, "noop", {}, delay_s=3600)  # not ready yet: counted, but not aged
    store.execute("UPDATE jobs SET run_after = run_after - 120 WHERE run_after < ?", [time.time()])
    m = scrape(c)
    assert sample(m, "tirkit_jobs", state="queued") == 3
    assert sample(m, "tirkit_jobs", state="running") == 0
    age = sample(m, "tirkit_jobs_oldest_ready_age_seconds")
    assert age is not None and 119 <= age < 600


def test_judge_spend(keyed) -> None:
    from tirkit.online import add_spend

    c, _, pid = keyed
    store = c.app.state.store  # type: ignore[attr-defined]
    add_spend(store.for_project(pid), 0.25)
    add_spend(store.for_project("other"), 0.5)
    store.execute(
        "INSERT INTO judge_spend (project_id, day, usd) VALUES (?, ?, ?)", [pid, "2000-01-01", 2.0]
    )
    m = scrape(c)
    assert sample(m, "tirkit_judge_spend_today_usd") == pytest.approx(0.75)
    assert sample(m, "tirkit_judge_spend_usd_total") == pytest.approx(2.75)
    assert sample(m, "tirkit_db_up") == 1


def test_buffer_depth_source(keyed) -> None:
    c, _, _ = keyed
    assert sample(scrape(c), "tirkit_ingest_buffer_depth") == 0
    set_buffer_depth_source(c.app, lambda: 7)
    assert sample(scrape(c), "tirkit_ingest_buffer_depth") == 7


def test_metrics_token(tmp_path) -> None:
    s = make_settings(tmp_path)
    s.metrics_token = "s3cret-token"
    with TestClient(create_app(s)) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert scrape(c, Authorization="Bearer s3cret-token")


def test_metrics_can_be_disabled(tmp_path) -> None:
    s = make_settings(tmp_path)
    s.metrics_enabled = False
    with TestClient(create_app(s)) as c:
        assert c.get("/metrics").status_code == 404
        assert c.get("/api/health").status_code == 200


def test_metrics_route_is_not_in_the_api_schema(keyed) -> None:
    c, _, _ = keyed
    assert "/metrics" not in c.get("/api/openapi.json").json()["paths"]


def test_two_processes_share_counters(tmp_path: Path) -> None:
    """With several server workers (``tirkit serve --workers N``) every scrape sees the sum."""
    mp = tmp_path / "prom"
    mp.mkdir()
    db = tmp_path / "server.db"
    child = textwrap.dedent(
        f"""
        import sys, json
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from fastapi.testclient import TestClient
        from tests.test_server import make_settings
        from tirkit.server.app import create_app
        from pathlib import Path
        s = make_settings(Path({str(tmp_path)!r}), db={str(db)!r})
        with TestClient(create_app(s)) as c:
            r = c.post("/api/v1/ingest", json={{"spans": []}})
            assert r.status_code == 401, r.text
            if sys.argv[1] == "scrape":
                print(json.dumps(c.get("/metrics").text))
        """
    )
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(mp)}
    for _ in range(2):
        subprocess.run([sys.executable, "-c", child, "ingest"], env=env, check=True)
    out = subprocess.run(
        [sys.executable, "-c", child, "scrape"], env=env, check=True, capture_output=True, text=True
    )
    m = parse(json.loads(out.stdout.strip().splitlines()[-1]))
    req = "tirkit_ingest_requests_total"
    assert sample(m, req, route="spans", outcome="rejected", reason="auth") == 3


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get("TIRKIT_TEST_POSTGRES_URL"), reason="TIRKIT_TEST_POSTGRES_URL not set"
)
def test_postgres_pool_saturation(tmp_path) -> None:
    psycopg = pytest.importorskip("psycopg")
    url = os.environ["TIRKIT_TEST_POSTGRES_URL"]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with TestClient(create_app(make_settings(tmp_path, db=url))) as c:
        m = scrape(c)
        assert sample(m, "tirkit_db_pool_max") == 16
        assert sample(m, "tirkit_db_pool_connections", state="in_use") is not None
        assert sample(m, "tirkit_db_pool_connections", state="idle") is not None
        assert sample(m, "tirkit_db_pool_waiting") == 0
        assert sample(m, "tirkit_jobs", state="queued") == 0


def test_serve_with_several_workers_sets_up_shared_metrics(tmp_path, monkeypatch) -> None:
    import uvicorn
    from typer.testing import CliRunner

    from tirkit.cli import app

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    for var in (
        "PROMETHEUS_MULTIPROC_DIR",
        "TIRKIT_EMBEDDED_WORKER",
        "TIRKIT_DATABASE_URL",
        "TIRKIT_INSECURE_COOKIES",
    ):
        monkeypatch.delenv(var, raising=False)
    res = CliRunner().invoke(
        app, ["serve", "--workers", "2", "--database-url", str(tmp_path / "x.db")]
    )
    assert res.exit_code == 0, res.output
    mp = Path(os.environ["PROMETHEUS_MULTIPROC_DIR"])
    assert mp.is_dir() and calls[0]["workers"] == 2

    # A directory given by the operator is reused, with stale files from a previous run removed.
    stale = tmp_path / "prom"
    stale.mkdir()
    (stale / "counter_123.db").write_bytes(b"old")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(stale))
    res = CliRunner().invoke(
        app, ["serve", "--workers", "2", "--database-url", str(tmp_path / "x.db")]
    )
    assert res.exit_code == 0, res.output
    assert list(stale.iterdir()) == []
