"""Tracing must fail open: a Tirkit server that is down, erroring or not answering must never take
the traced app down, slow its calls, or hold it up when it exits (SPEC 3.1)."""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


APP = textwrap.dedent(
    """
    import sys, time
    import tirkit

    t0 = time.perf_counter()
    tirkit.init(host=sys.argv[1], public_key="pk-tirkit-x", secret_key="sk-tirkit-x")
    init_s = time.perf_counter() - t0
    worst = 0.0
    for i in range(300):
        t = time.perf_counter()
        with tirkit.span("step", kind="agent", input={"i": i}) as s:
            s.output = "ok"
        worst = max(worst, time.perf_counter() - t)
    print(f"init_s={init_s:.3f} worst_step_s={worst:.4f}", flush=True)
    """
)


def run_app(host: str, timeout: float = 60.0) -> tuple[subprocess.CompletedProcess[str], float]:
    t0 = time.perf_counter()
    done = subprocess.run(
        [sys.executable, "-c", APP, host],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"), "TIRKIT_NO_COLOR": "1"},
    )
    return done, time.perf_counter() - t0


def test_a_refused_connection_does_not_crash_the_app_or_slow_it() -> None:
    done, total = run_app(f"http://127.0.0.1:{closed_port()}")
    assert done.returncode == 0, done.stderr[-1500:]
    fields = dict(p.split("=") for p in done.stdout.split())
    assert float(fields["init_s"]) < 1.0  # init makes no network call
    assert float(fields["worst_step_s"]) < 0.25  # a traced step never waits for the network
    assert total < 15.0  # and the app exits promptly


def test_init_does_not_touch_the_network() -> None:
    import tirkit

    t0 = time.perf_counter()
    client = tirkit.init(
        host=f"http://127.0.0.1:{closed_port()}", public_key="pk-x", secret_key="sk-x"
    )
    try:
        assert time.perf_counter() - t0 < 1.0
    finally:
        client.exporter.shutdown(timeout=1.0)


@pytest.mark.parametrize("mode", ["hang"])
def test_a_server_that_never_answers_does_not_hold_the_app_up(mode: str) -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(50)
        port = srv.getsockname()[1]
        done, total = run_app(f"http://127.0.0.1:{port}")  # accepts connections, never replies
    assert done.returncode == 0, done.stderr[-1500:]
    fields = dict(p.split("=") for p in done.stdout.split())
    assert float(fields["init_s"]) < 1.0
    assert float(fields["worst_step_s"]) < 0.25
    assert total < 15.0, f"the app took {total:.1f}s to exit with a hanging server"


class _Down:
    """A target that is down until told otherwise."""

    def __init__(self) -> None:
        self.up = False
        self.saved = 0

    def save_spans(self, spans: list[object]) -> None:
        if not self.up:
            raise ConnectionError("down")
        self.saved += len(spans)


def test_an_outage_is_logged_once_without_tracebacks_and_recovery_is_noted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from tirkit.exporter import BatchExporter
    from tirkit.tracing import Span

    monkeypatch.setattr(time, "sleep", lambda s: None)  # no real backoff waits
    target = _Down()
    exp = BatchExporter(target, max_retries=1)
    with caplog.at_level(logging.INFO, logger="tirkit"):
        for _ in range(5):
            exp._write([Span(name="a")])
        down = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(down) == 1, [r.getMessage() for r in down]
        assert down[0].exc_info is None  # no traceback in the application's logs
        assert exp.failed == 5  # every lost batch is still counted
        target.up = True
        exp._write([Span(name="a")])
        assert any("exporting again" in r.getMessage() for r in caplog.records)
        target.up = False
        exp._write([Span(name="a")])
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2  # a new outage
    exp.shutdown(timeout=1.0)
