"""Trace detail split (plan A3): a light span tree with short previews, one full span on demand,
explicit paging for huge traces, and ``?full=1`` for the old shape."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_server import basic, make_settings, new_key, setup_admin  # noqa: E402
from tirkit.server.app import create_app  # noqa: E402
from tirkit.tracing import Span  # noqa: E402

pytestmark = pytest.mark.server


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(tmp_path))) as c:
        yield c

BIG = "x" * 50_000

LIGHT_KEYS = {
    "span_id",
    "parent_id",
    "name",
    "kind",
    "status",
    "level",
    "start",
    "end",
    "duration_ms",
    "ttft_ms",
    "model",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "error",
    "input_preview",
    "output_preview",
    "input_truncated",
    "output_truncated",
}


def _ingest(c: TestClient, key: dict[str, str], spans: list[Span]) -> None:
    for i in range(0, len(spans), 1000):
        body = {"spans": [s.to_dict() for s in spans[i : i + 1000]]}
        r = c.post("/api/v1/ingest", json=body, headers=key)
        assert r.status_code == 200, r.text


def _trace(n_children: int = 3) -> list[Span]:
    now = time.time()
    root = Span(name="agent", start=now, input={"q": "hi", "doc": BIG}, output="short answer")
    root.end = now + 2
    spans = [root]
    for i in range(n_children):
        child = Span(
            name=f"llm-{i}",
            trace_id=root.trace_id,
            parent_id=root.span_id,
            kind="llm",
            model="gpt-4o-mini",
            input=[{"role": "user", "content": "hello"}],
            output=BIG if i == 0 else "ok",
            start=now + 0.01 * (i + 1),
            model_params={"temperature": 0},
            attributes={"k": "v"},
        )
        child.end = child.start + 0.5
        child.set_usage(input_tokens=10, output_tokens=5)
        spans.append(child)
    spans[-1].record_error("E" * 10_000)
    return spans


@pytest.fixture
def seeded(client: TestClient) -> tuple[TestClient, str, list[Span]]:
    pid, _ = setup_admin(client)
    key = basic(*new_key(client, pid))
    spans = _trace()
    _ingest(client, key, spans)
    return client, pid, spans


def test_default_detail_is_light(seeded) -> None:
    c, pid, spans = seeded
    r = c.get(f"/api/projects/{pid}/traces/{spans[0].trace_id}")
    assert r.status_code == 200
    body = r.json()
    assert len(r.content) < 30_000  # the 50 KB input and output are not in it
    assert body["trace"]["trace_id"] == spans[0].trace_id and body["trace"]["n_spans"] == 4
    assert "input" not in body["trace"] and "output" not in body["trace"]
    assert body["total_spans"] == 4 and body["omitted"] == 0 and body["next_offset"] is None
    assert body["preview"] == 2000
    rows = body["spans"]
    assert [s["name"] for s in rows] == ["agent", "llm-0", "llm-1", "llm-2"]  # by start time
    assert all(set(s) == LIGHT_KEYS for s in rows)
    root, llm0, _, llm2 = rows
    assert root["parent_id"] is None and llm0["parent_id"] == root["span_id"]
    assert llm0["input_tokens"] == 10 and llm0["output_tokens"] == 5 and llm0["cost_usd"] > 0
    assert root["input_truncated"] and len(root["input_preview"]) <= 2000
    assert root["input_preview"].startswith('{"q"')
    assert not root["output_truncated"] and root["output_preview"] == "short answer"
    assert llm0["output_truncated"] and llm0["output_preview"] == "x" * 2000
    assert len(llm2["error"]) <= 2000 and llm2["status"] == "error"
    assert "model_params" not in llm0 and "attributes" not in llm0


def test_preview_length_is_adjustable(seeded) -> None:
    c, pid, spans = seeded
    rows = c.get(
        f"/api/projects/{pid}/traces/{spans[0].trace_id}", params={"preview": 100}
    ).json()["spans"]
    assert len(rows[1]["output_preview"]) == 100 and rows[1]["output_truncated"]
    rows = c.get(
        f"/api/projects/{pid}/traces/{spans[0].trace_id}", params={"preview": 0}
    ).json()["spans"]
    assert rows[0]["output_preview"] is None and rows[0]["input_preview"] is None


def test_one_full_span(seeded) -> None:
    c, pid, spans = seeded
    llm0 = spans[1]
    r = c.get(f"/api/projects/{pid}/traces/{llm0.trace_id}/spans/{llm0.span_id}")
    assert r.status_code == 200
    full = r.json()
    assert full["output"] == BIG and full["model_params"] == {"temperature": 0}
    assert full["attributes"] == {"k": "v"} and full["input"][0]["content"] == "hello"
    missing = c.get(f"/api/projects/{pid}/traces/{llm0.trace_id}/spans/nope")
    assert missing.status_code == 404
    other_trace = c.get(f"/api/projects/{pid}/traces/{'0' * 32}/spans/{llm0.span_id}")
    assert other_trace.status_code == 404


def test_full_flag_keeps_the_old_shape(seeded) -> None:
    c, pid, spans = seeded
    for flag in ("1", "true"):
        body = c.get(
            f"/api/projects/{pid}/traces/{spans[0].trace_id}", params={"full": flag}
        ).json()
        assert set(body) == {"trace", "spans", "scores"}
        assert body["spans"][0]["input"]["doc"] == BIG
        assert body["spans"][1]["model_params"] == {"temperature": 0}


def test_huge_trace_is_paged_with_an_omitted_marker(client: TestClient) -> None:
    pid, _ = setup_admin(client)
    key = basic(*new_key(client, pid))
    spans = _trace(n_children=2_499)  # 2,500 spans
    _ingest(client, key, spans)
    url = f"/api/projects/{pid}/traces/{spans[0].trace_id}"
    page = client.get(url, params={"limit": 1000}).json()
    assert len(page["spans"]) == 1000 and page["total_spans"] == 2500
    assert page["omitted"] == 1500 and page["next_offset"] == 1000
    seen = [s["span_id"] for s in page["spans"]]
    while page["next_offset"] is not None:
        page = client.get(url, params={"limit": 1000, "offset": page["next_offset"]}).json()
        seen += [s["span_id"] for s in page["spans"]]
    assert page["omitted"] == 0 and page["offset"] == 2000
    assert sorted(seen) == sorted(s.span_id for s in spans)  # every span exactly once

    default = client.get(url).json()  # default page holds a 10,000-span trace in one request
    assert len(default["spans"]) == 2500 and default["omitted"] == 0
    # Previews share a fixed budget per page, so a big trace stays light: 2,500 spans with
    # 50 KB outputs come back in well under a megabyte of previews.
    assert default["preview"] == 200
    assert max(len(s["output_preview"] or "") for s in default["spans"]) == 200


def test_limits_are_clamped(seeded) -> None:
    c, pid, spans = seeded
    url = f"/api/projects/{pid}/traces/{spans[0].trace_id}"
    body: dict[str, Any] = c.get(url, params={"limit": 10**9, "preview": 10**9}).json()
    assert len(body["spans"]) == 4
    assert len(body["spans"][1]["output_preview"]) <= 20_000
    assert len(c.get(url, params={"limit": 0}).json()["spans"]) == 1  # at least one per page
    assert c.get(url, params={"offset": 99}).json()["total_spans"] == 4
