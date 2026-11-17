"""Server security (B2-B4): login throttling, ingest limits, headers and CORS."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_server import PASSWORD, basic, make_settings, new_key, setup_admin  # noqa: E402
from tirkit.server.app import create_app  # noqa: E402
from tirkit.server.security import client_ip  # noqa: E402

pytestmark = pytest.mark.server

EMAIL = "admin@example.com"


def _client(tmp_path: Any, **overrides: Any) -> TestClient:
    s = make_settings(tmp_path)
    for k, v in overrides.items():
        setattr(s, k, v)
    return TestClient(create_app(s))


@pytest.fixture
def anon(tmp_path: Any) -> Iterator[TestClient]:
    with _client(tmp_path) as c:
        setup_admin(c)
        c.cookies.clear()
        yield c


def _login(c: TestClient, password: str, email: str = EMAIL, **kw: Any) -> Any:
    return c.post("/api/auth/login", json={"email": email, "password": password}, **kw)


# -- B2 login throttling -----------------------------------------------------------------------


def test_login_throttle_per_account_is_progressive_and_generic(anon: TestClient) -> None:
    codes = [_login(anon, "wrong password!!").status_code for _ in range(5)]
    assert codes == [401] * 5
    r = _login(anon, "wrong password!!")
    assert r.status_code == 429
    assert r.json() == {"detail": "too many sign-in attempts; try again later"}
    assert int(r.headers["retry-after"]) >= 1
    # The right password is also refused while the delay runs (no oracle for guessing).
    assert _login(anon, PASSWORD).status_code == 429
    # An unknown account gets the same treatment, so throttling does not reveal which exist.
    other = [_login(anon, "x" * 12, email="nobody@example.com").status_code for _ in range(6)]
    assert other == [401] * 5 + [429]


def test_login_success_resets_account_count(anon: TestClient) -> None:
    for _ in range(4):
        assert _login(anon, "wrong password!!").status_code == 401
    assert _login(anon, PASSWORD).status_code == 200
    anon.cookies.clear()
    assert [_login(anon, "wrong password!!").status_code for _ in range(5)] == [401] * 5


def test_login_delay_expires(anon: TestClient, monkeypatch: Any) -> None:
    for _ in range(6):
        _login(anon, "wrong password!!")
    assert _login(anon, PASSWORD).status_code == 429
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 5)  # the first delay is 1 s
    assert _login(anon, PASSWORD).status_code == 200


def test_login_throttle_per_ip(tmp_path: Any) -> None:
    with _client(tmp_path, login_free_per_ip=3, trusted_proxies=1) as c:
        setup_admin(c)
        c.cookies.clear()
        ip = {"X-Forwarded-For": "203.0.113.9"}
        codes = [
            _login(c, "wrong password!!", email=f"u{i}@example.com", headers=ip).status_code
            for i in range(4)
        ]
        assert codes == [401, 401, 401, 429]
        # Another address is unaffected.
        r = _login(
            c, "wrong password!!", email="z@example.com", headers={"X-Forwarded-For": "1.2.3.4"}
        )
        assert r.status_code == 401


def test_login_global_limit(tmp_path: Any) -> None:
    with _client(tmp_path, login_global_per_s=0.1) as c:  # burst of 1
        setup_admin(c)
        c.cookies.clear()
        assert _login(c, "wrong password!!", email="a@example.com").status_code == 401
        assert _login(c, "wrong password!!", email="b@example.com").status_code == 429


class _Req:
    def __init__(self, xff: str | None, peer: str = "10.0.0.2") -> None:
        self.headers = {"x-forwarded-for": xff} if xff is not None else {}
        self.client = type("C", (), {"host": peer})()


@pytest.mark.parametrize(
    "xff,trusted,expected",
    [
        ("6.6.6.6", 0, "10.0.0.2"),  # no trusted proxy: forwarding headers are ignored
        ("6.6.6.6, 198.51.100.7", 1, "198.51.100.7"),  # spoofed left entry is skipped
        ("198.51.100.7, 10.0.0.9", 2, "198.51.100.7"),
        ("198.51.100.7", 2, "198.51.100.7"),  # fewer hops than proxies: leftmost
        (None, 1, "10.0.0.2"),
    ],
)
def test_client_ip_trusted_proxies(xff: str | None, trusted: int, expected: str) -> None:
    assert client_ip(_Req(xff), trusted) == expected  # type: ignore[arg-type]


# -- B3 ingest limits --------------------------------------------------------------------------


def _spans(n: int, trace: str = "t1") -> bytes:
    now = time.time()
    return json.dumps(
        {
            "spans": [
                {"trace_id": trace, "span_id": f"{trace}-{i}", "name": "s", "start": now}
                for i in range(n)
            ]
        }
    ).encode()


def test_ingest_limit_returns_429_with_retry_after(tmp_path: Any) -> None:
    with _client(tmp_path, ingest_spans_per_s=10, ingest_burst=20) as c:
        pid, _ = setup_admin(c)
        pk, sk = new_key(c, pid)
        c.cookies.clear()
        h = {**basic(pk, sk), "content-type": "application/json"}
        assert c.post("/api/v1/ingest", content=_spans(15), headers=h).status_code == 200
        r = c.post("/api/v1/ingest", content=_spans(15, "t2"), headers=h)
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1
        assert "rate limit" in r.json()["detail"]
        # Another key on the same project has its own bucket.
        pk2, sk2 = new_key_with_session(c, pid)
        h2 = {**basic(pk2, sk2), "content-type": "application/json"}
        assert c.post("/api/v1/ingest", content=_spans(15, "t3"), headers=h2).status_code == 200
        # A batch larger than the burst is charged as a full bucket, not refused forever.
        assert c.post("/api/v1/ingest", content=_spans(50, "t4"), headers=h2).status_code == 429


def new_key_with_session(c: TestClient, pid: str) -> tuple[str, str]:
    r = c.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 200
    c.headers["X-CSRF-Token"] = c.get("/api/auth/me").json()["csrf"]
    pk, sk = new_key(c, pid)
    c.cookies.clear()
    return pk, sk


def test_ingest_limit_disabled_with_zero(tmp_path: Any) -> None:
    with _client(tmp_path, ingest_spans_per_s=0) as c:
        pid, _ = setup_admin(c)
        pk, sk = new_key(c, pid)
        h = {**basic(pk, sk), "content-type": "application/json"}
        for i in range(3):
            assert c.post("/api/v1/ingest", content=_spans(500, f"t{i}"), headers=h).is_success


def test_sdk_backs_off_on_429_and_delivers(tmp_path: Any, live_server: Any) -> None:
    """The exporter retries a 429 after Retry-After instead of dropping the batch."""
    import tirkit
    from tirkit.http import HttpClient

    url, app = live_server
    c = TestClient(app)
    pid, _ = setup_admin(c)
    pk, sk = new_key(c, pid)
    from tirkit.ratelimit import MemoryBackend, RateLimiter, Rule

    app.state.limits.ingest = RateLimiter("ingest", Rule(4, 1.0, burst=4), MemoryBackend())
    http = HttpClient(url, public_key=pk, secret_key=sk)
    exp = tirkit.exporter.BatchExporter(http, batch_size=4, interval_s=0.05, max_retries=5)
    from tirkit.tracing import Span

    for i in range(8):
        exp.save_span(Span(name="s", trace_id="tr", span_id=f"s{i}"))
    assert exp.flush(timeout=20)
    stats = exp.stats()
    exp.shutdown()
    assert stats["failed"] == 0 and stats["dropped"] == 0 and stats["exported"] == 8, stats


# -- B4 headers and CORS -----------------------------------------------------------------------


def test_security_headers_on_api_and_ui(anon: TestClient) -> None:
    for path in ("/api/health", "/", "/api/auth/me"):
        r = anon.get(path)
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert "strict-transport-security" not in r.headers  # plain HTTP


def test_hsts_only_on_https(tmp_path: Any) -> None:
    with _client(tmp_path, trusted_proxies=1) as c:
        r = c.get("/api/health", headers={"X-Forwarded-Proto": "https"})
        assert r.headers["strict-transport-security"].startswith("max-age=")
    with _client(tmp_path / "b") as c:  # no trusted proxy: the header is ignored
        r = c.get("/api/health", headers={"X-Forwarded-Proto": "https"})
        assert "strict-transport-security" not in r.headers
    with TestClient(create_app(make_settings(tmp_path / "c")), base_url="https://testserver") as c:
        assert "strict-transport-security" in c.get("/api/health").headers


def test_media_keeps_its_sandbox_csp(tmp_path: Any) -> None:
    import hashlib

    with _client(tmp_path) as c:
        pid, _ = setup_admin(c)
        pk, sk = new_key(c, pid)
        data = b"<svg onload=alert(1)>"
        sha = hashlib.sha256(data).hexdigest()
        assert c.put(f"/api/v1/media/{sha}", content=data, headers=basic(pk, sk)).status_code == 201
        r = c.get(f"/api/projects/{pid}/media/{sha}")
        assert r.headers["content-security-policy"] == "sandbox"


def test_cors_same_origin_by_default(anon: TestClient) -> None:
    pre = {
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization, content-type",
    }
    r = anon.options("/api/v1/ingest", headers=pre)
    assert "access-control-allow-origin" not in r.headers
    r = anon.get("/api/health", headers={"Origin": "https://app.example.com"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_origins_for_ingestion_only(tmp_path: Any) -> None:
    with _client(tmp_path, cors_origins=["https://app.example.com"]) as c:
        pid, _ = setup_admin(c)
        pk, sk = new_key(c, pid)
        c.cookies.clear()
        pre = {
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        }
        r = c.options("/api/v1/ingest", headers=pre)
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == "https://app.example.com"
        assert "authorization" in r.headers["access-control-allow-headers"]
        assert "access-control-allow-credentials" not in r.headers
        h = {
            **basic(pk, sk),
            "content-type": "application/json",
            "Origin": "https://app.example.com",
        }
        r = c.post("/api/v1/ingest", content=_spans(1), headers=h)
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "https://app.example.com"
        # Other origins and the session API stay same-origin.
        evil = {**pre, "Origin": "https://evil.example"}
        assert (
            "access-control-allow-origin" not in c.options("/api/v1/ingest", headers=evil).headers
        )
        r = c.options("/api/auth/login", headers=pre)
        assert "access-control-allow-origin" not in r.headers


def test_ui_shell_works_under_the_csp() -> None:
    """The CSP has no 'unsafe-inline': the UI's HTML must not need inline script, inline style
    blocks or event-handler attributes. Checks the source page and, when built, the built one."""
    import re
    from pathlib import Path

    from tirkit.server.security import CSP

    assert "script-src 'self'" in CSP and "unsafe-inline" not in CSP
    root = Path(__file__).resolve().parents[1] / "web"
    pages = [root / "index.html"] + (
        [root / "dist" / "index.html"] if (root / "dist").exists() else []
    )
    for page in pages:
        html = page.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html), page  # inline script
        assert "<style" not in html and not re.search(r"\son\w+\s*=", html), page
        assert not re.search(r"<(?:script|link)[^>]+(?:src|href)=[\"']https?://", html), page
