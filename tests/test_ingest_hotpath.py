"""Ingestion on the hot path: it must not block the event loop (plan A2), and re-sending a batch
must not change any count (plan A6, the SDK retries batches)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_server import basic, make_settings, new_key, setup_admin  # noqa: E402
from tirkit.server.app import create_app  # noqa: E402
from tirkit.store import SQLStore  # noqa: E402
from tirkit.tracing import Span  # noqa: E402

pytestmark = pytest.mark.server


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(tmp_path))) as c:
        yield c

SLOW_S = 1.0
HEALTH_BOUND_S = 0.25  # generous for a busy CI machine; a blocked loop takes SLOW_S or more


def _batch(n_traces: int = 3, children: int = 4) -> list[dict[str, Any]]:
    now = time.time()
    out = []
    for t in range(n_traces):
        root = Span(name=f"root-{t}", start=now + t, input={"q": t}, output="a", user_id="u")
        root.end = root.start + 1
        out.append(root.to_dict())
        for i in range(children):
            child = Span(
                name=f"llm-{i}",
                trace_id=root.trace_id,
                parent_id=root.span_id,
                kind="llm",
                model="gpt-4o-mini",
                start=root.start + 0.1 * i,
            )
            child.end = child.start + 0.05
            child.set_usage(input_tokens=100, output_tokens=10)
            out.append(child.to_dict())
    return out


def _otlp_json(trace_hex: str = "ab" * 16) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_hex,
                                "spanId": f"{i:016x}",
                                "name": f"otel-{i}",
                                "startTimeUnixNano": str(1_700_000_000_000_000_000 + i),
                                "endTimeUnixNano": str(1_700_000_000_500_000_000 + i),
                            }
                            for i in range(1, 4)
                        ]
                    }
                ],
            }
        ]
    }


@pytest.mark.parametrize("route", ["/api/v1/ingest", "/v1/traces"])
def test_health_stays_fast_during_slow_save_spans(tmp_path, monkeypatch, route) -> None:
    original = SQLStore.save_spans
    started = threading.Event()

    def slow_save_spans(self: SQLStore, spans: Any) -> None:
        started.set()
        time.sleep(SLOW_S)
        original(self, spans)

    monkeypatch.setattr(SQLStore, "save_spans", slow_save_spans)
    with TestClient(create_app(make_settings(tmp_path))) as c:
        pid, _ = setup_admin(c)
        key = basic(*new_key(c, pid))
        body = {"spans": _batch(1, 1)} if route == "/api/v1/ingest" else _otlp_json()
        results: list[Any] = []
        worker = threading.Thread(
            target=lambda: results.append(c.post(route, json=body, headers=key))
        )
        worker.start()
        assert started.wait(5), "ingest never reached save_spans"
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            assert c.get("/api/health").status_code == 200
            times.append(time.perf_counter() - t0)
        assert max(times) < HEALTH_BOUND_S, f"health took {max(times) * 1000:.0f} ms"
        worker.join(10)
        assert results and results[0].status_code == 200, results


def _counts(c: TestClient, pid: str) -> dict[str, Any]:
    st = c.app.state.store.for_project(pid)  # type: ignore[attr-defined]
    traces = sorted(
        (t.trace_id, t.n_spans, t.input_tokens, t.output_tokens, round(t.cost_usd or 0, 12))
        for t in st.query_traces(limit=1000)
    )
    return {"spans": st.count_spans(), "traces": st.count_traces(), "summaries": traces}


def test_resending_a_batch_leaves_counts_unchanged(client: TestClient) -> None:
    pid, _ = setup_admin(client)
    key = basic(*new_key(client, pid))
    batch = _batch()
    assert client.post("/api/v1/ingest", json={"spans": batch}, headers=key).status_code == 200
    first = _counts(client, pid)
    assert first["spans"] == 15 and first["traces"] == 3
    assert all(n == 5 and tin == 400 for _, n, tin, _, _ in first["summaries"])

    # The same batch again, the same batch reversed, and split in two (a retry after a timeout
    # where the first attempt was in fact stored).
    assert client.post("/api/v1/ingest", json={"spans": batch}, headers=key).status_code == 200
    rev = {"spans": list(reversed(batch))}
    assert client.post("/api/v1/ingest", json=rev, headers=key).status_code == 200
    for half in (batch[:7], batch[7:]):
        assert client.post("/api/v1/ingest", json={"spans": half}, headers=key).status_code == 200
    assert _counts(client, pid) == first

    # Scores are not part of the batch: a resend does not touch them either.
    tid = first["summaries"][0][0]
    client.post("/api/v1/scores", json={"name": "thumbs", "value": 1, "trace_id": tid}, headers=key)
    n_scores = len(client.get(f"/api/projects/{pid}/traces/{tid}").json()["scores"])
    assert client.post("/api/v1/ingest", json={"spans": batch}, headers=key).status_code == 200
    assert len(client.get(f"/api/projects/{pid}/traces/{tid}").json()["scores"]) == n_scores


def test_resending_an_otlp_batch_leaves_counts_unchanged(client: TestClient) -> None:
    pid, _ = setup_admin(client)
    key = {**basic(*new_key(client, pid)), "content-type": "application/json"}
    body = json.dumps(_otlp_json())
    assert client.post("/v1/traces", content=body, headers=key).status_code == 200
    first = _counts(client, pid)
    assert first["spans"] == 3 and first["traces"] == 1
    assert client.post("/v1/traces", content=body, headers=key).status_code == 200
    assert _counts(client, pid) == first


def test_resending_through_the_store_leaves_counts_unchanged(store) -> None:
    spans = [Span.from_dict(d) for d in _batch()]
    store.save_spans(spans)
    before = (store.count_spans(), store.count_traces(), store.query_traces())
    store.save_spans(spans)
    store.save_spans(spans[::-1])
    assert (store.count_spans(), store.count_traces(), store.query_traces()) == before
