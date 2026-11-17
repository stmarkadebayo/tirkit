"""Background exporter: batching, media extraction, drops, flush."""

from __future__ import annotations

import time

from tests.conftest import ListSink
from tirkit.exporter import BatchExporter
from tirkit.media import MemoryMediaStore
from tirkit.tracing import Span, span

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 3000


def test_spans_are_written_in_background_with_media() -> None:
    target = ListSink()
    media = MemoryMediaStore()
    exp = BatchExporter(target, media_store=media, interval_s=0.05)
    with span("a", exp, input={"img": PNG}):
        pass
    assert exp.flush(5)
    assert target.spans[0].input["img"]["type"] == "media"
    assert len(media.blobs) == 1
    exp.shutdown()


def test_full_queue_drops_without_blocking() -> None:
    class Slow(ListSink):
        def save_spans(self, spans):
            time.sleep(0.2)
            super().save_spans(spans)

    exp = BatchExporter(Slow(), max_queue=5, batch_size=5, interval_s=10)
    start = time.perf_counter()
    for i in range(50):
        exp.save_span(Span(name=str(i)))
    assert time.perf_counter() - start < 0.1
    assert exp.dropped >= 40
    exp.shutdown()


def test_failing_target_is_retried_then_counted() -> None:
    class Flaky(ListSink):
        calls = 0

        def save_spans(self, spans):
            Flaky.calls += 1
            if Flaky.calls < 2:
                raise OSError("busy")
            super().save_spans(spans)

    target = Flaky()
    exp = BatchExporter(target, interval_s=0.01)
    exp.save_span(Span(name="x"))
    assert exp.flush(5)
    assert [s.name for s in target.spans] == ["x"] and exp.failed == 0
    exp.shutdown()


def test_init_local_mode_writes_sqlite(tmp_path) -> None:
    import tirkit

    client = tirkit.init(path=tmp_path / "m.db")
    with tirkit.span("hello", kind="llm", model="gpt-4o-mini", input="hi") as s:
        s.output = "yo"
        s.set_usage(input_tokens=3, output_tokens=1)
    tirkit.flush()
    got = client.store.query_spans()
    assert got[0].name == "hello" and got[0].cost_usd > 0
    client.shutdown()
