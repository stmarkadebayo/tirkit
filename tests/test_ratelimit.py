"""Rate limiter (B1): token bucket, sliding window and progressive delay on both backends."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from tirkit.ratelimit import (
    DatabaseBackend,
    MemoryBackend,
    ProgressiveDelay,
    RateLimiter,
    Rule,
)
from tirkit.store import SQLiteStore, open_store


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def backend(request: Any, tmp_path: Any) -> Iterator[Any]:
    if request.param == "memory":
        yield MemoryBackend()
        return
    if request.param == "postgres":
        url = os.environ.get("TIRKIT_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("TIRKIT_TEST_POSTGRES_URL not set")
        st = open_store(url)
        st.init_schema()
        st.execute("DELETE FROM rate_limits")
        yield DatabaseBackend(st)
        st.close()
        return
    st = SQLiteStore(tmp_path / "rl.db")
    st.init_schema()
    yield DatabaseBackend(st)
    st.close()


# (rule, [(t, cost, expect_allowed)])
TOKEN_BUCKET_CASES = [
    # 4 per second, burst 3: three immediate hits pass, the fourth waits 0.25 s.
    (
        Rule(4, 1, burst=3),
        [(0, 1, True), (0, 1, True), (0, 1, True), (0, 1, False), (0.25, 1, True)],
    ),
    # Cost larger than one token: 5 at once from a bucket of 5, then refill.
    (Rule(5, 1), [(0, 5, True), (0.5, 3, False), (0.75, 3, True)]),
    # A cost above the capacity is never allowed.
    (Rule(5, 1), [(0, 6, False), (100, 6, False)]),
    # Refill never exceeds capacity.
    (Rule(2, 1), [(0, 2, True), (1000, 2, True), (1000, 1, False)]),
]


@pytest.mark.parametrize("rule,steps", TOKEN_BUCKET_CASES)
def test_token_bucket(backend: Any, rule: Rule, steps: list[tuple[float, float, bool]]) -> None:
    lim = RateLimiter("tb", rule, backend)
    t0 = 1_700_000_000.0
    for t, cost, ok in steps:
        d = lim.hit("k", cost=cost, now=t0 + t)
        assert d.allowed is ok, (t, cost, d)
        if not ok and cost <= (rule.burst or rule.limit):
            assert d.retry_after > 0


SLIDING_CASES = [
    # 3 per 10 s window.
    (Rule(3, 10, algorithm="sliding_window"), [(0, True), (1, True), (2, True), (3, False)]),
    # Next window: the previous window's count still weighs in proportionally.
    # Window [0,10): 3 hits; at t=15 the estimate is 3*0.5 + 0 = 1.5, so one more fits.
    (
        Rule(3, 10, algorithm="sliding_window"),
        [(0, True), (1, True), (2, True), (15, True), (15, False), (19.9, True)],
    ),
    # Two windows later everything is forgotten.
    (Rule(1, 10, algorithm="sliding_window"), [(0, True), (5, False), (30, True)]),
]


@pytest.mark.parametrize("rule,steps", SLIDING_CASES)
def test_sliding_window(backend: Any, rule: Rule, steps: list[tuple[float, bool]]) -> None:
    lim = RateLimiter("sw", rule, backend)
    t0 = 1_700_000_000.0  # a multiple of 10, so windows start at t0
    for t, ok in steps:
        d = lim.hit("k", now=t0 + t)
        assert d.allowed is ok, (t, d)
        if not ok:
            assert d.retry_after > 0


def test_keys_and_limiters_are_independent(backend: Any) -> None:
    a = RateLimiter("a", Rule(1, 60), backend)
    b = RateLimiter("b", Rule(1, 60), backend)
    assert a.hit("x", now=1.0).allowed
    assert not a.hit("x", now=1.0).allowed
    assert a.hit("y", now=1.0).allowed
    assert b.hit("x", now=1.0).allowed


def test_progressive_delay(backend: Any) -> None:
    pd = ProgressiveDelay("login", free=3, base_s=1, cap_s=8, reset_after_s=100, backend=backend)
    t = 1_700_000_000.0
    for _ in range(3):
        assert pd.attempt("acct", now=t).allowed
    # Three attempts are free; after that each one must wait, doubling up to the cap.
    assert not pd.attempt("acct", now=t).allowed
    d = pd.attempt("acct", now=t + 0.5)
    assert not d.allowed and 0 < d.retry_after <= 1
    assert pd.attempt("acct", now=t + 1).allowed  # waited the base delay
    assert not pd.attempt("acct", now=t + 2.5).allowed  # now needs 2 s
    assert pd.attempt("acct", now=t + 3).allowed
    assert pd.attempt("acct", now=t + 7).allowed  # 4 s
    assert not pd.attempt("acct", now=t + 14).allowed  # 8 s (cap) needed, only 7 passed
    assert pd.attempt("acct", now=t + 15).allowed
    assert not pd.attempt("acct", now=t + 22).allowed  # still capped at 8 s
    # A long quiet period resets the count.
    for i in range(3):
        assert pd.attempt("acct", now=t + 200 + i * 0.01).allowed
    # Success clears the key.
    pd.reset("acct")
    for _ in range(3):
        assert pd.attempt("acct", now=t + 300).allowed


def test_progressive_delay_refund(backend: Any) -> None:
    pd = ProgressiveDelay("ip", free=2, base_s=10, cap_s=10, reset_after_s=1000, backend=backend)
    t = 1_700_000_000.0
    assert pd.attempt("ip", now=t).allowed
    pd.refund("ip")  # a successful login does not count against the IP
    assert pd.attempt("ip", now=t).allowed
    assert pd.attempt("ip", now=t).allowed
    assert not pd.attempt("ip", now=t).allowed


def test_two_app_instances_share_one_database(tmp_path: Any) -> None:
    """Two processes (here: two stores on one SQLite file) see each other's hits."""
    db = tmp_path / "shared.db"
    s1, s2 = SQLiteStore(db), SQLiteStore(db)
    s1.init_schema()
    s2.init_schema()
    l1 = RateLimiter("ingest", Rule(4, 60), DatabaseBackend(s1))
    l2 = RateLimiter("ingest", Rule(4, 60), DatabaseBackend(s2))
    t = 1_700_000_000.0
    results = [lim.hit("key", now=t).allowed for lim in (l1, l2, l1, l2, l1, l2)]
    assert results == [True, True, True, True, False, False]
    s1.close()
    s2.close()


@pytest.mark.server
def test_two_server_instances_share_login_throttle(tmp_path: Any) -> None:
    """Two app instances on one database: failures on one throttle logins on the other."""
    from fastapi.testclient import TestClient

    from tests.test_server import PASSWORD, make_settings
    from tirkit.server.app import create_app

    db = str(tmp_path / "shared.db")
    s = make_settings(tmp_path, db)
    with TestClient(create_app(s)) as a, TestClient(create_app(make_settings(tmp_path, db))) as b:
        assert a.post(
            "/api/setup", json={"email": "x@example.com", "password": PASSWORD}
        ).is_success
        a.cookies.clear()
        bad = {"email": "x@example.com", "password": "wrong password!!"}
        codes = [
            (a if i % 2 else b).post("/api/auth/login", json=bad).status_code for i in range(8)
        ]
        assert 429 in codes, codes
        r = b.post("/api/auth/login", json={"email": "x@example.com", "password": PASSWORD})
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


# -- the SDK side of B3 ------------------------------------------------------------------------


class _Throttled:
    """A target that answers 429 (with Retry-After) a few times, then accepts."""

    def __init__(self, times: int, retry_after: float | None = 3.0) -> None:
        from tirkit.http import RateLimited

        self.left = times
        self.err = RateLimited(retry_after) if retry_after is not None else RuntimeError("down")
        self.saved: list[Any] = []

    def save_spans(self, spans: list[Any]) -> None:
        if self.left > 0:
            self.left -= 1
            raise self.err
        self.saved.extend(spans)


def test_exporter_honours_retry_after_without_using_retries(monkeypatch: Any) -> None:
    import time as _time

    from tirkit.exporter import BatchExporter
    from tirkit.tracing import Span

    sleeps: list[float] = []
    monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
    target = _Throttled(times=5)  # more 429s than max_retries (3): still delivered
    exp = BatchExporter(target, max_retries=3)
    exp._write([Span(name="a")])
    exp.shutdown()
    assert len(target.saved) == 1 and exp.failed == 0 and exp.throttled == 5
    assert all(3.0 <= s <= 3.3 for s in sleeps if s >= 1)  # the server's wait, plus jitter


def test_exporter_gives_up_when_the_wait_is_too_long(monkeypatch: Any) -> None:
    import time as _time

    from tirkit.exporter import BatchExporter
    from tirkit.tracing import Span

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    target = _Throttled(times=10_000, retry_after=30)  # never recovers
    exp = BatchExporter(target, max_retries=3)
    monkeypatch.setattr("tirkit.exporter.MAX_RATE_LIMIT_WAIT_S", 0.0)
    exp._write([Span(name="a")])
    exp.shutdown()
    assert exp.failed == 1 and target.saved == []  # counted, not silently dropped


def test_other_errors_still_use_the_normal_retries(monkeypatch: Any) -> None:
    import time as _time

    from tirkit.exporter import BatchExporter
    from tirkit.tracing import Span

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    target = _Throttled(times=2, retry_after=None)
    exp = BatchExporter(target, max_retries=3)
    exp._write([Span(name="a")])
    exp.shutdown()
    assert len(target.saved) == 1 and exp.throttled == 0
