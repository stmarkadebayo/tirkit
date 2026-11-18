"""Fork safety (plan A1): a process forked after tracing started keeps exporting its own spans.

Each child runs with a deadline; a child that hangs is killed and the test fails instead of
hanging the suite.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from tirkit.exporter import BatchExporter
from tirkit.tracing import Span, span

pytestmark = pytest.mark.skipif(
    not hasattr(os, "fork") or sys.platform == "win32", reason="needs os.fork"
)


class FileSink:
    """Appends span names to a file, one JSON line per span, opened per write (fork-safe)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def save_spans(self, spans: list[Span]) -> None:
        with self.path.open("a") as f:
            for s in spans:
                f.write(json.dumps({"name": s.name, "pid": os.getpid()}) + "\n")


def names(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(line)["name"] for line in path.read_text().splitlines()]


def wait_child(pid: int, timeout: float = 10.0) -> int:
    """Exit code of ``pid``; kills it and fails if it has not exited within ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    pytest.fail("child process deadlocked")


def fork_and_run(fn) -> int:  # noqa: ANN001
    pid = os.fork()
    if pid == 0:  # child
        code = 1
        try:
            code = 0 if fn() else 2
        finally:
            os._exit(code)
    return wait_child(pid)


@pytest.fixture(autouse=True)
def _quiet_fork_warning():  # noqa: ANN202
    # Python 3.12+ warns that fork() in a multi-threaded process may deadlock: that is the
    # exact situation these tests exercise.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


def test_spans_created_in_forked_child_reach_target(tmp_path: Path) -> None:
    out = tmp_path / "spans.jsonl"
    exp = BatchExporter(FileSink(out), interval_s=0.01)
    with span("parent-before", exp):
        pass
    assert exp.flush(5)

    def child() -> bool:
        with span("child", exp):
            pass
        return exp.flush(5)

    assert fork_and_run(child) == 0
    with span("parent-after", exp):
        pass
    assert exp.flush(5)
    assert sorted(names(out)) == ["child", "parent-after", "parent-before"]
    exp.shutdown()


def test_child_does_not_reexport_parent_queue(tmp_path: Path) -> None:
    out = tmp_path / "spans.jsonl"
    exp = BatchExporter(FileSink(out), interval_s=60, batch_size=10_000)
    exp.save_span(Span(name="queued-in-parent"))  # not yet written when the fork happens

    def child() -> bool:
        exp.save_span(Span(name="child"))
        return exp.flush(5)

    assert fork_and_run(child) == 0
    assert exp.flush(5)
    assert sorted(names(out)) == ["child", "queued-in-parent"]
    exp.shutdown()


def test_no_deadlock_when_parent_holds_lock_at_fork(tmp_path: Path) -> None:
    out = tmp_path / "spans.jsonl"
    exp = BatchExporter(FileSink(out), interval_s=0.01)

    def child() -> bool:
        exp.save_span(Span(name="child"))
        return exp.flush(5)

    with exp._lock:  # another thread is inside save_span when the fork happens
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                code = 0 if child() else 2
            finally:
                os._exit(code)
    assert wait_child(pid) == 0
    assert names(out) == ["child"]
    exp.shutdown()


def test_no_deadlock_when_exporter_thread_is_writing_at_fork(tmp_path: Path) -> None:
    out = tmp_path / "spans.jsonl"

    class SlowSink(FileSink):
        def save_spans(self, spans: list[Span]) -> None:
            time.sleep(0.3)
            super().save_spans(spans)

    exp = BatchExporter(SlowSink(out), interval_s=0.01)
    exp.save_span(Span(name="slow-parent"))
    time.sleep(0.05)  # the exporter thread is now inside save_spans

    def child() -> bool:
        exp.save_span(Span(name="child"))
        return exp.flush(5)

    assert fork_and_run(child) == 0
    assert exp.flush(5)
    assert sorted(names(out)) == ["child", "slow-parent"]
    exp.shutdown()


def _mp_worker(i: int) -> int:
    from tirkit import tracing

    with tracing.span(f"worker-{i}"):
        pass
    return i  # no explicit flush: the exporter flushes when the worker process exits


def test_multiprocessing_fork_pool_spans_reach_target(tmp_path: Path) -> None:
    from tirkit.tracing import set_default_sink

    out = tmp_path / "spans.jsonl"
    exp = BatchExporter(FileSink(out), interval_s=60, batch_size=10_000)
    set_default_sink(exp)
    try:
        ctx = multiprocessing.get_context("fork")
        with ctx.Pool(2) as pool:
            assert sorted(pool.map(_mp_worker, range(4))) == [0, 1, 2, 3]
            pool.close()
            pool.join()
    finally:
        set_default_sink(None)
    assert sorted(names(out)) == [f"worker-{i}" for i in range(4)]
    exp.shutdown()


def _mp_process(path: str) -> None:
    from tirkit import tracing

    with tracing.span("process-child"):
        pass


def test_multiprocessing_fork_process_spans_reach_target(tmp_path: Path) -> None:
    from tirkit.tracing import set_default_sink

    out = tmp_path / "spans.jsonl"
    exp = BatchExporter(FileSink(out), interval_s=60, batch_size=10_000)
    set_default_sink(exp)
    try:
        p = multiprocessing.get_context("fork").Process(target=_mp_process, args=(str(out),))
        p.start()
        p.join(10)
        if p.is_alive():
            p.kill()
            pytest.fail("child process deadlocked")
        assert p.exitcode == 0
    finally:
        set_default_sink(None)
    assert names(out) == ["process-child"]
    exp.shutdown()


def test_local_sqlite_mode_survives_fork(tmp_path: Path) -> None:
    import tirkit

    client = tirkit.init(path=tmp_path / "fork.db")
    with tirkit.span("parent"):
        pass
    assert tirkit.flush()

    def child() -> bool:
        with tirkit.span("child"):
            pass
        return tirkit.flush(5)

    with client.store._lock:  # the parent is mid-transaction when the fork happens
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                code = 0 if child() else 2
            finally:
                os._exit(code)
    assert wait_child(pid) == 0
    assert sorted(s.name for s in client.store.query_spans()) == ["child", "parent"]
    client.shutdown()


def test_http_client_gets_a_fresh_connection_pool_in_child() -> None:
    pytest.importorskip("httpx")
    from tirkit.http import HttpClient

    hc = HttpClient("http://127.0.0.1:9", public_key="pk", secret_key="sk")
    parent_client = hc.client
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        os.write(w, b"1" if hc.client is not parent_client else b"0")
        os._exit(0)
    os.close(w)
    assert wait_child(pid) == 0
    assert os.read(r, 1) == b"1"
    os.close(r)
    assert hc.client is parent_client
