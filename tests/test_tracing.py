"""Span model, nesting, decorator and context propagation."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import tirkit
from tirkit.tracing import span, trace


def test_nested_spans_share_trace_and_parent(sink) -> None:
    with span("outer") as outer:
        with span("inner", kind="tool") as inner:
            pass
    assert inner.trace_id == outer.trace_id
    assert inner.parent_id == outer.span_id
    assert outer.parent_id is None
    assert [s.name for s in sink.spans] == ["inner", "outer"]


def test_error_is_recorded_and_reraised(sink) -> None:
    with pytest.raises(ValueError):
        with span("boom"):
            raise ValueError("bad")
    s = sink.by_name("boom")
    assert s.status == "error"
    assert "ValueError: bad" in s.error


def test_decorator_captures_input_and_output(sink) -> None:
    @trace(kind="tool")
    def add(a: int, b: int = 2) -> int:
        return a + b

    assert add(1, b=5) == 6
    s = sink.spans[0]
    assert s.kind == "tool"
    assert s.input == {"a": 1, "b": 5}
    assert s.output == 6
    assert s.name.endswith("add")


def test_decorator_without_parentheses_and_generators(sink) -> None:
    @trace
    def gen(n: int):
        yield from range(n)

    assert list(gen(3)) == [0, 1, 2]
    assert sink.spans[0].output == [0, 1, 2]


def test_async_tasks_keep_their_parents(sink) -> None:
    @trace(name="leaf")
    async def leaf(i: int) -> int:
        await asyncio.sleep(0.001 * (3 - i))
        return i

    async def main() -> None:
        with span("root") as root:
            await asyncio.gather(*(leaf(i) for i in range(3)))
        return root

    root = asyncio.run(main())
    leaves = [s for s in sink.spans if s.name == "leaf"]
    assert len(leaves) == 3
    assert all(s.parent_id == root.span_id and s.trace_id == root.trace_id for s in leaves)


def test_async_generator(sink) -> None:
    @trace(name="agen")
    async def agen():
        for i in range(2):
            yield i

    async def main() -> list[int]:
        return [x async for x in agen()]

    assert asyncio.run(main()) == [0, 1]
    assert sink.by_name("agen").output == [0, 1]


def test_threads_get_parent_with_copy_context_run(sink) -> None:
    def work() -> None:
        with span("child"):
            pass

    with span("root") as root:
        with ThreadPoolExecutor(2) as pool:
            pool.submit(tirkit.copy_context_run(work)).result()
        # A bare thread starts a new trace (no context copy).
        t = threading.Thread(target=work)
        t.start()
        t.join()
    children = [s for s in sink.spans if s.name == "child"]
    assert children[0].parent_id == root.span_id
    assert children[1].parent_id is None


def test_trace_context_sets_session_user_and_tags(sink) -> None:
    with tirkit.trace_context(session_id="s1", user_id="u1", tags=["beta"]):
        with span("a"):
            with span("b"):
                pass
    assert {(s.session_id, s.user_id, tuple(s.tags)) for s in sink.spans} == {
        ("s1", "u1", ("beta",))
    }


def test_cost_is_computed_from_usage(sink) -> None:
    with span("call", kind="llm", model="gpt-4o-mini") as s:
        s.set_usage(input_tokens=1000, output_tokens=500)
    assert sink.spans[0].cost_usd == pytest.approx(1000 * 1.5e-7 + 500 * 6e-7)


def test_sink_failure_never_reaches_caller() -> None:
    class Broken:
        def save_span(self, span):
            raise RuntimeError("disk full")

    with span("ok", Broken()):
        pass  # no exception


def test_kwargs_become_attributes_and_model(sink) -> None:
    with span("llm.call", model="x", temperature=0.2):
        pass
    s = sink.spans[0]
    assert s.model == "x"
    assert s.attributes == {"model": "x", "temperature": 0.2}
