"""SDK retry (plan A5): Retry-After, jittered exponential backoff and a bound on total retry time,
against a fake server that answers 429 and 503."""

from __future__ import annotations

import email.utils
import json
import random
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

pytest.importorskip("httpx")

from tirkit.exporter import BatchExporter, backoff_delay  # noqa: E402
from tirkit.http import HttpClient, RetryableHTTPError, parse_retry_after  # noqa: E402
from tirkit.tracing import Span  # noqa: E402


class FakeServer:
    """Answers ``POST /api/v1/ingest`` from a script of (status, headers) and records requests."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str]]] = []
        self.requests: list[tuple[float, list[str]]] = []  # (time, span names)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                names = [s["name"] for s in json.loads(body)["spans"]]
                server.requests.append((time.monotonic(), names))
                status, headers = server.script.pop(0) if server.script else (200, {})
                payload = b'{"accepted": %d}' % len(names) if status == 200 else b"{}"
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def accepted(self) -> list[str]:
        """Span names from requests that the script answered with 200 (the last ones)."""
        return self.requests[-1][1] if self.requests else []


@pytest.fixture
def fake() -> Iterator[FakeServer]:
    srv = FakeServer()
    yield srv
    srv.httpd.shutdown()


def exporter(fake: FakeServer, **kw: Any) -> BatchExporter:
    http = HttpClient(fake.url, public_key="pk", secret_key="sk", timeout=5)
    return BatchExporter(http, interval_s=0.01, **kw)


def test_429_honours_retry_after(fake: FakeServer) -> None:
    fake.script = [(429, {"Retry-After": "1"})]
    exp = exporter(fake)
    exp.save_span(Span(name="a"))
    assert exp.flush(10)
    assert len(fake.requests) == 2 and fake.accepted() == ["a"]
    waited = fake.requests[1][0] - fake.requests[0][0]
    assert 0.95 <= waited < 2.0  # Retry-After: 1, plus at most a little jitter
    assert exp.stats()["exported"] == 1 and exp.failed == 0
    exp.shutdown()


def test_503_without_retry_after_backs_off_then_succeeds(fake: FakeServer) -> None:
    fake.script = [(503, {}), (503, {})]
    exp = exporter(fake)
    exp.save_span(Span(name="b"))
    assert exp.flush(10)
    assert len(fake.requests) == 3 and fake.accepted() == ["b"] and exp.failed == 0
    exp.shutdown()


def test_retry_time_is_bounded(fake: FakeServer) -> None:
    fake.script = [(503, {})] * 1000
    exp = exporter(fake, max_retries=1000, max_retry_s=1.0)
    exp.save_span(Span(name="c"))
    start = time.monotonic()
    assert exp.flush(10)
    assert time.monotonic() - start < 2.5
    assert exp.failed == 1 and exp.exported == 0
    exp.shutdown()


def test_retry_after_beyond_budget_gives_up_without_sleeping(fake: FakeServer) -> None:
    fake.script = [(429, {"Retry-After": "3600"})]
    exp = exporter(fake, max_retry_s=2.0)
    exp.save_span(Span(name="d"))
    start = time.monotonic()
    assert exp.flush(10)
    assert time.monotonic() - start < 1.0
    assert len(fake.requests) == 1 and exp.failed == 1
    exp.shutdown()


def test_client_errors_are_not_retried(fake: FakeServer) -> None:
    fake.script = [(400, {})]
    exp = exporter(fake)
    exp.save_span(Span(name="e"))
    assert exp.flush(10)
    assert len(fake.requests) == 1 and exp.failed == 1
    exp.shutdown()


def test_save_spans_raises_retryable_error_with_retry_after(fake: FakeServer) -> None:
    fake.script = [(503, {"Retry-After": "7"})]
    http = HttpClient(fake.url, public_key="pk", secret_key="sk")
    with pytest.raises(RetryableHTTPError) as info:
        http.save_spans([Span(name="f")])
    assert info.value.status == 503 and info.value.retry_after == 7.0


def test_connection_refused_is_retryable() -> None:
    http = HttpClient("http://127.0.0.1:9", public_key="pk", secret_key="sk", timeout=1)
    with pytest.raises(RetryableHTTPError) as info:
        http.save_spans([Span(name="g")])
    assert info.value.status is None and info.value.retry_after is None


def test_parse_retry_after() -> None:
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(" 0 ") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-5") is None
    future = email.utils.formatdate(time.time() + 30, usegmt=True)
    assert 25 <= (parse_retry_after(future) or 0) <= 31
    past = email.utils.formatdate(time.time() - 30, usegmt=True)
    assert parse_retry_after(past) == 0.0


def test_backoff_is_jittered_exponential_and_capped() -> None:
    rng = random.Random(1)
    for attempt in range(12):
        ceiling = min(5.0, 0.2 * 2**attempt)
        delays = [backoff_delay(attempt, rng=rng) for _ in range(200)]
        assert all(0 <= d <= ceiling for d in delays)
        assert max(delays) - min(delays) > ceiling * 0.5  # spread out, not one fixed value
