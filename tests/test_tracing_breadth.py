"""Phase G tracing breadth: span kinds, sampling rules, retry and fallback spans,
time to first token and reasoning tokens."""

from __future__ import annotations

import asyncio

import pytest

import tirkit
from tirkit.resilience import fallback, retry, with_retry
from tirkit.tracing import KINDS, set_sampling, span


@pytest.fixture(autouse=True)
def _no_sampling():
    yield
    set_sampling(None)


def test_new_span_kinds_are_known() -> None:
    for kind in ("vector_db", "rerank", "mcp_tool", "human", "computer_use", "retry", "fallback"):
        assert kind in KINDS


def test_mark_first_token_sets_ttft_once(sink) -> None:
    with span("gen", kind="llm") as s:
        s.start -= 0.2
        s.mark_first_token()
        first = s.ttft_ms
        s.mark_first_token()
    assert first is not None and 150 < first < 1000
    assert sink.spans[0].ttft_ms == first


# -- sampling -------------------------------------------------------------------------------


def test_sampling_keeps_whole_traces_or_none(sink) -> None:
    set_sampling(rate=0.5)
    for i in range(200):
        with span(f"root{i}"):
            with span("child"):
                pass
    by_trace: dict[str, int] = {}
    for s in sink.spans:
        by_trace[s.trace_id] = by_trace.get(s.trace_id, 0) + 1
    assert set(by_trace.values()) == {2}  # never a partial trace
    assert 40 < len(by_trace) < 160  # roughly half kept


def test_sampling_is_deterministic_per_trace_id(sink) -> None:
    from tirkit.tracing import Sampler

    sampler = Sampler(rate=0.3)
    ids = [f"{i:032x}" for i in range(1000)]
    first = [sampler.keep_trace(t, "x", "generic") for t in ids]
    assert first == [sampler.keep_trace(t, "x", "generic") for t in ids]
    assert 200 < sum(first) < 400


def test_sampling_rules_match_root_name_and_kind(sink) -> None:
    set_sampling(
        rate=1.0,
        rules=[{"name": "health*", "rate": 0.0}, {"kind": "agent", "rate": 1.0}],
    )
    with span("healthcheck"):
        with span("db"):
            pass
    with span("answer", kind="agent"):
        pass
    with span("other"):
        pass
    assert [s.name for s in sink.spans] == ["answer", "other"]


def test_sampling_zero_still_exports_errors(sink) -> None:
    set_sampling(rate=0.0)
    with span("quiet"):
        pass
    with pytest.raises(ValueError):
        with span("root"):
            with span("ok-before"):
                pass
            with span("boom"):
                raise ValueError("x")
    names = [s.name for s in sink.spans]
    # The failing span and every span that finishes after it in that trace are kept.
    assert names == ["boom", "root"]
    assert sink.spans[0].status == "error"


def test_sampling_errors_can_be_dropped_too(sink) -> None:
    set_sampling(rate=0.0, keep_errors=False)
    with pytest.raises(ValueError):
        with span("root"):
            raise ValueError("x")
    assert sink.spans == []


def test_sampling_rate_validation() -> None:
    with pytest.raises(ValueError):
        set_sampling(rate=1.5)
    with pytest.raises(ValueError):
        set_sampling(rules=[{"name": "x", "rate": -1}])


def test_init_reads_sample_rate(tmp_path, monkeypatch, sink) -> None:
    from tirkit import tracing

    monkeypatch.setenv("TIRKIT_SAMPLE_RATE", "0.25")
    client = tirkit.init(path=tmp_path / "t.db")
    try:
        assert tracing.get_sampler() is not None and tracing.get_sampler().rate == 0.25
        tirkit.init(path=tmp_path / "t.db", sample_rate=1.0)
        assert tracing.get_sampler() is None  # rate 1 with no rules: sampling off
    finally:
        tirkit.get_client().shutdown()
        del client


# -- retry and fallback ---------------------------------------------------------------------


def test_retry_records_each_attempt(sink) -> None:
    calls = {"n": 0}

    @retry(attempts=3, retry_on=(ConnectionError,))
    def flaky(x: int) -> int:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("reset")
        return x * 2

    assert flaky(21) == 42
    parent = next(s for s in sink.spans if s.kind == "retry")
    attempts = [s for s in sink.spans if s.parent_id == parent.span_id]
    assert [a.attributes["retry.attempt"] for a in attempts] == [1, 2, 3]
    assert [a.status for a in attempts] == ["error", "error", "ok"]
    assert parent.status == "ok" and parent.attributes["retry.attempts"] == 3
    assert parent.attributes["retry.max_attempts"] == 3 and parent.output == 42


def test_retry_gives_up_and_does_not_retry_other_errors(sink) -> None:
    with pytest.raises(ConnectionError):
        with_retry(_always(ConnectionError("down")), attempts=2, retry_on=(ConnectionError,))
    parent = next(s for s in sink.spans if s.kind == "retry")
    assert parent.status == "error" and parent.attributes["retry.attempts"] == 2
    sink.spans.clear()
    with pytest.raises(KeyError):
        with_retry(_always(KeyError("k")), attempts=5, retry_on=(ConnectionError,))
    parent = next(s for s in sink.spans if s.kind == "retry")
    assert parent.attributes["retry.attempts"] == 1


def test_retry_async_and_nested_llm_span(sink) -> None:
    n = {"i": 0}

    @retry(attempts=2, name="call-model")
    async def call() -> str:
        n["i"] += 1
        with span("llm", kind="llm"):
            if n["i"] == 1:
                raise TimeoutError("slow")
        return "ok"

    assert asyncio.run(call()) == "ok"
    parent = sink.by_name("call-model")
    attempts = [s for s in sink.spans if s.parent_id == parent.span_id]
    llms = [s for s in sink.spans if s.kind == "llm"]
    assert {s.parent_id for s in llms} == {a.span_id for a in attempts}


def test_fallback_uses_the_first_that_works(sink) -> None:
    def primary(q: str) -> str:
        raise RuntimeError("rate limited")

    def secondary(q: str) -> str:
        return q.upper()

    answer = fallback(primary, secondary, name="answer")
    assert answer("hi") == "HI"
    parent = sink.by_name("answer")
    assert parent.kind == "fallback" and parent.attributes["fallback.chosen"] == 1
    assert parent.attributes["fallback.chosen_name"] == "secondary"
    options = [s for s in sink.spans if s.parent_id == parent.span_id]
    assert [(o.attributes["fallback.index"], o.status) for o in options] == [
        (0, "error"),
        (1, "ok"),
    ]


def test_fallback_all_fail_raises_last_error(sink) -> None:
    f = fallback(_always(RuntimeError("a")), _always(ValueError("b")))
    with pytest.raises(ValueError):
        f()
    parent = next(s for s in sink.spans if s.kind == "fallback")
    assert parent.status == "error" and parent.attributes.get("fallback.chosen") is None


def test_fallback_async(sink) -> None:
    async def bad() -> str:
        raise RuntimeError("x")

    async def good() -> str:
        return "ok"

    assert asyncio.run(fallback(bad, good)()) == "ok"


def _always(exc: BaseException):
    def fn(*a, **k):
        raise exc

    return fn


# -- dashboard metrics: cache hit rate, reasoning tokens, time to first token ----------------


def test_metrics_cache_hit_rate_reasoning_and_ttft(store) -> None:
    import time

    from tirkit.tracing import Span

    now = time.time()
    spans = []
    for i, (cached, ttft) in enumerate([(0, 100.0), (50, 200.0), (100, 300.0), (None, None)]):
        s = Span(name="chat", kind="llm", model="gpt-4o", start=now - 60 + i)
        s.end = s.start + 1
        s.set_usage(input_tokens=100, output_tokens=10, cached_input_tokens=cached)
        if i == 1:
            s.set_usage(reasoning_tokens=4)
        s.ttft_ms = ttft
        spans.append(s)
    store.save_spans(spans)
    m = store.metrics(now - 120, now + 1, buckets=1)
    (point,) = m["series"]["all"]
    assert point["input_tokens"] == 400 and point["cached_input_tokens"] == 150
    assert point["cache_hit_rate"] == pytest.approx(150 / 400)
    assert point["reasoning_tokens"] == 4
    assert point["ttft_p50_ms"] == pytest.approx(200.0)
    assert point["ttft_p95_ms"] == pytest.approx(290.0)
    empty = store.metrics(now + 10, now + 20, buckets=1)
    assert empty["series"] == {}
