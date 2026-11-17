"""SQLite store: round trips, filters, trace aggregation, project isolation."""

from __future__ import annotations

import time

from tirkit.store import SQLiteStore
from tirkit.tracing import Span


def _span(**kw) -> Span:
    base = dict(name="call", start=time.time(), end=time.time() + 0.1)
    base.update(kw)
    return Span(**base)


def test_round_trip_all_fields(store) -> None:
    s = _span(
        kind="llm",
        model="gpt-4o",
        input={"messages": [{"role": "user", "content": "hi"}]},
        output={"content": "hello"},
        usage={"input_tokens": 10, "output_tokens": 3},
        cost_usd=0.001,
        session_id="s",
        user_id="u",
        release="v1",
        tags=["a", "b"],
        attributes={"k": 1},
        ttft_ms=12.5,
    )
    store.save_span(s)
    got = store.query_spans()[0]
    assert got.input == s.input and got.output == s.output
    assert got.input_tokens == 10 and got.output_tokens == 3
    assert got.tags == ["a", "b"] and got.attributes == {"k": 1}
    assert got.ttft_ms == 12.5 and got.release == "v1"


def test_filters(store) -> None:
    now = time.time()
    store.save_spans(
        [
            _span(name="a", model="m1", start=now - 100, end=now - 99, tags=["x"]),
            _span(
                name="b", model="m2", start=now, end=now + 1, status="error", attributes={"k": "v"}
            ),
        ]
    )
    assert [s.name for s in store.query_spans({"model": "m1"})] == ["a"]
    assert [s.name for s in store.query_spans({"error": True})] == ["b"]
    assert [s.name for s in store.query_spans({"start_after": now - 10})] == ["b"]
    assert [s.name for s in store.query_spans({"tag": "x"})] == ["a"]
    assert [s.name for s in store.query_spans({"attributes": {"k": "v"}})] == ["b"]
    assert store.count_spans() == 2


def test_search(store) -> None:
    store.save_span(_span(input={"q": "the capital of France"}))
    store.save_span(_span(input={"q": "weather"}))
    assert len(store.query_spans({"search": "France"})) == 1


def test_trace_summary_aggregates_spans(store) -> None:
    root = _span(name="root", input="question", output="answer", session_id="s1")
    child = _span(
        name="llm",
        trace_id=root.trace_id,
        parent_id=root.span_id,
        usage={"input_tokens": 100, "output_tokens": 20},
        cost_usd=0.5,
        status="error",
    )
    store.save_span(child)  # children usually finish first
    store.save_span(root)
    store.save_span(child)  # re-delivery must not double count
    t = store.get_trace(root.trace_id)
    assert t is not None
    assert t.name == "root" and t.input == "question" and t.session_id == "s1"
    assert t.n_spans == 2 and t.input_tokens == 100 and t.cost_usd == 0.5
    assert t.has_error
    assert [s.name for s in store.get_spans(root.trace_id)] == [
        s.name for s in sorted([root, child], key=lambda x: x.start)
    ]


def test_projects_are_isolated(store) -> None:
    other = store.for_project("p2")
    other.save_span(_span(name="secret"))
    store.save_span(_span(name="mine"))
    assert [s.name for s in store.query_spans()] == ["mine"]
    assert [s.name for s in other.query_spans()] == ["secret"]
    assert store.count_traces() == 1


def test_init_schema_is_idempotent(tmp_path) -> None:
    st = SQLiteStore(tmp_path / "x.db")
    st.init_schema()
    st.init_schema()
    assert st.query_spans() == []


def test_full_text_search_is_indexed_and_word_based(store) -> None:
    from tirkit.tracing import Span as S

    root = S(
        name="agent",
        input={"question": "How long does shipping take to Nairobi?"},
        output="3 to 5 days",
    )
    child = S(
        name="llm",
        trace_id=root.trace_id,
        parent_id=root.span_id,
        input="internal prompt",
        output="Réponse en français",
    )
    other = S(name="agent", input={"question": "battery life"}, output="90 minutes")
    store.save_spans([root, child, other])
    assert [s.span_id for s in store.query_spans({"search": "nairobi"})] == [root.span_id]
    assert [s.span_id for s in store.query_spans({"search": "ship"})] == [root.span_id]  # prefix
    assert [s.span_id for s in store.query_spans({"search": "reponse francais"})] == [
        child.span_id
    ]  # accents folded
    assert store.query_spans({"search": "shipping battery"}) == []  # every word must match
    # A trace matches when any of its spans does.
    assert [t.trace_id for t in store.query_traces({"search": "francais"})] == [root.trace_id]
    # Updates and deletes keep the index in sync.
    root.output = "now ships overnight"
    store.save_span(root)
    assert store.query_spans({"search": "overnight"})[0].span_id == root.span_id
    store.delete_before(10**12)
    assert store.query_spans({"search": "overnight"}) == []
    # Symbols-only queries fall back to substring matching.
    store.save_span(S(name="x", input="price: $$$"))
    assert len(store.query_spans({"search": "$$$"})) == 1


def test_search_index_backfills_existing_rows(tmp_path) -> None:
    import tirkit.store as st

    db = SQLiteStore(tmp_path / "old.db")
    saved = st.MIGRATIONS
    try:
        st.MIGRATIONS = saved[:-1]  # a database created before the search index existed
        db.init_schema()
        db.save_span(
            Span(name="legacy", input="vintage question about warranties", start=1.0, end=2.0)
        )
    finally:
        st.MIGRATIONS = saved
    db.init_schema()
    assert [s.name for s in db.query_spans({"search": "warrant"})] == ["legacy"]
