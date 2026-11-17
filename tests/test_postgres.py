"""The same store and server behaviour on Postgres. Needs TIRKIT_TEST_POSTGRES_URL pointing at a
scratch database (every table is dropped first), e.g. the CI service container."""

from __future__ import annotations

import os
import time
import uuid

import pytest

URL = os.environ.get("TIRKIT_TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="TIRKIT_TEST_POSTGRES_URL not set"),
]


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg")
    from tirkit.store import PostgresStore

    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    st = PostgresStore(URL)
    st.init_schema()
    yield st
    st.close()


def test_span_round_trip_filters_and_traces(pg) -> None:
    from tirkit.tracing import Span

    now = time.time()
    root = Span(
        name="root",
        start=now,
        end=now + 1,
        input="question",
        output={"a": 1},
        session_id="s1",
        user_id="u1",
        tags=["x"],
    )
    child = Span(
        name="llm",
        trace_id=root.trace_id,
        parent_id=root.span_id,
        start=now + 0.1,
        end=now + 0.5,
        model="gpt-4o",
        usage={"input_tokens": 10, "output_tokens": 2},
        cost_usd=0.01,
        status="error",
        attributes={"k": "v", "n": 3},
    )
    pg.save_spans([child, root])
    pg.save_span(child)
    t = pg.get_trace(root.trace_id)
    assert t.n_spans == 2 and t.input_tokens == 10 and t.has_error and t.input == "question"
    assert [s.name for s in pg.query_spans({"tag": "x"})] == ["root"]
    assert [s.name for s in pg.query_spans({"attributes": {"k": "v"}})] == ["llm"]
    assert [s.name for s in pg.query_spans({"attributes": {"n": 3}})] == ["llm"]
    assert [s.name for s in pg.query_spans({"search": "QUESTION"})] == ["root"]
    assert pg.query_spans({"name": "root"})[0].output == {"a": 1}
    assert pg.for_project("other").count_spans() == 0
    assert pg.sessions()[0]["session_id"] == "s1"
    m = pg.metrics(now - 10, now + 10, buckets=2)
    assert sum(p["spans"] for p in m["series"]["all"]) == 2


def test_datasets_experiments_scores(pg) -> None:
    from tirkit.datasets import Dataset, Example
    from tirkit.runner import RunResult

    ds = Dataset("d", [Example({"q": "a"}, "A"), Example({"q": "b"}, None, {"m": 1})])
    v = pg.save_dataset(ds)
    assert pg.load_dataset("d", v).examples == ds.examples
    res = RunResult(
        "d",
        v,
        outputs=["A", None],
        scores={"s": [1.0, None]},
        errors=[None, "boom"],
        trace_ids=[uuid.uuid4().hex, None],
        reasons={"s": [None, "target failed"]},
    )
    pg.save_experiment(res)
    back = pg.load_experiment(res.experiment_id)
    assert back.scores == {"s": [1.0, None]} and back.errors == [None, "boom"]
    pg.save_score("thumbs", 1.0, trace_id="t1", source="user_feedback")
    assert pg.query_scores(trace_id="t1")[0]["value"] == 1.0


@pytest.mark.server
def test_server_on_postgres(tmp_path, pg) -> None:
    from fastapi.testclient import TestClient

    from tests.test_server import basic, make_settings, new_key, setup_admin
    from tirkit.server.app import create_app

    pg.close()
    with TestClient(create_app(make_settings(tmp_path, URL))) as c:
        pid, _ = setup_admin(c)
        pk, sk = new_key(c, pid)
        from tirkit.tracing import Span

        s = Span(name="x", input="hi", model="gpt-4o-mini", usage={"input_tokens": 5})
        s.end = s.start + 0.01
        assert (
            c.post(
                "/api/v1/ingest", json={"spans": [s.to_dict()]}, headers=basic(pk, sk)
            ).status_code
            == 200
        )
        assert c.get(f"/api/projects/{pid}/traces").json()["total"] == 1


def test_full_text_search_on_postgres(pg) -> None:
    from tirkit.tracing import Span

    root = Span(
        name="agent",
        input={"question": "How long does shipping take to Nairobi?"},
        output="3 to 5 days",
    )
    child = Span(
        name="llm", trace_id=root.trace_id, parent_id=root.span_id, output="Answer about refunds"
    )
    pg.save_spans([root, child, Span(name="agent", input="battery life")])
    assert [s.span_id for s in pg.query_spans({"search": "nairob"})] == [root.span_id]
    assert pg.query_spans({"search": "shipping battery"}) == []
    assert [t.trace_id for t in pg.query_traces({"search": "refunds"})] == [root.trace_id]
