"""SSRF guard (B5): every refused form, DNS pinning, redirects, size cap and the opt-out."""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from tirkit import netguard
from tirkit.netguard import BlockedURL, check_url, post_json


@pytest.fixture(autouse=True)
def _strict(monkeypatch: Any) -> None:
    monkeypatch.delenv(netguard.ALLOW_PRIVATE_ENV, raising=False)


BLOCKED = [
    "http://127.0.0.1/hook",
    "http://localhost/hook",
    "http://localhost:8080/hook",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/",
    "http://[64:ff9b::a9fe:a9fe]/",  # NAT64 form of 169.254.169.254
    "http://[2002:7f00:1::1]/",  # 6to4 around 127.0.0.1
    "http://[fd00:ec2::254]/",  # AWS IPv6 metadata (unique local)
    "http://[fe80::1]/",
    "http://0x7f.1/",
    "http://0x7f000001/",
    "http://2130706433/",  # decimal 127.0.0.1
    "http://017700000001/",  # octal
    "http://0177.0.0.1/",
    "http://127.1/",
    "http://0.0.0.0/",
    "http://10.0.0.5/",
    "http://172.16.3.4/",
    "http://192.168.1.1/",
    "http://100.64.0.1/",  # carrier-grade NAT
    "http://100.100.100.200/",  # Alibaba metadata (CGNAT range)
    "http://224.0.0.1/",
    "http://240.0.0.1/",
    "http://a@b.example.com/",
    "http://user:pass@hooks.example.com/",
    "http://hooks.example.com\\@127.0.0.1/",
    "http://hooks.example .com/",
    "ftp://hooks.example.com/",
    "file:///etc/passwd",
    "gopher://127.0.0.1:6379/_FLUSHALL",
    "http:///nohost",
    "http://[fe80::1%25en0]/",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_blocked_targets(url: str) -> None:
    with pytest.raises(BlockedURL):
        check_url(url)


def test_dns_name_resolving_to_private_is_blocked(monkeypatch: Any) -> None:
    monkeypatch.setattr(netguard, "resolve", lambda host, port: [ipaddress.ip_address("10.1.2.3")])
    with pytest.raises(BlockedURL, match="private"):
        check_url("https://internal.example.com/hook")


def test_any_private_address_blocks(monkeypatch: Any) -> None:
    """A name with one public and one private address is refused (no lucky picks)."""
    addrs = [ipaddress.ip_address("93.184.215.14"), ipaddress.ip_address("127.0.0.1")]
    monkeypatch.setattr(netguard, "resolve", lambda host, port: addrs)
    with pytest.raises(BlockedURL):
        check_url("https://mixed.example.com/")


def test_public_target_allowed(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        netguard, "resolve", lambda host, port: [ipaddress.ip_address("93.184.215.14")]
    )
    target, ips = check_url("https://hooks.example.com:8443/x?y=1")
    assert ips == [ipaddress.ip_address("93.184.215.14")]
    assert target.host_header == "hooks.example.com:8443" and target.path == "/x?y=1"
    assert check_url("https://8.8.8.8/")[1] == [ipaddress.ip_address("8.8.8.8")]
    # Hex-looking DNS labels are ordinary names.
    assert check_url("https://deadbeef.cafe/", dns=False)[1] == []


def test_save_time_check_without_dns() -> None:
    with pytest.raises(BlockedURL):
        check_url("http://localhost./", dns=False)
    with pytest.raises(BlockedURL):
        check_url("http://169.254.169.254/", dns=False)
    assert check_url("https://hooks.slack.com/services/x", dns=False)[1] == []


def test_opt_out_allows_private(monkeypatch: Any) -> None:
    monkeypatch.setenv(netguard.ALLOW_PRIVATE_ENV, "1")
    assert check_url("http://127.0.0.1:9/")[1] == [ipaddress.ip_address("127.0.0.1")]
    # Syntax rules still apply with the opt-out.
    for url in ("http://a@127.0.0.1/", "http://0x7f.1/", "file:///etc/passwd"):
        with pytest.raises(BlockedURL):
            check_url(url)


# -- delivery against a real local server ------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:
        return None

    def do_POST(self) -> None:
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n)
        _Handler.seen.append({"path": self.path, "host": self.headers.get("host"), "body": body})
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        reply = b"x" * (200_000 if self.path == "/big" else 2)
        self.send_response(200)
        self.send_header("content-length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


@pytest.fixture
def local_http() -> Iterator[int]:
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _Handler.seen.clear()
    yield srv.server_address[1]
    srv.shutdown()


def test_post_blocked_without_opt_out(local_http: int) -> None:
    with pytest.raises(BlockedURL):
        post_json(f"http://127.0.0.1:{local_http}/hook", {"text": "hi"})
    assert _Handler.seen == []


def test_post_connects_to_validated_ip_with_original_host(
    local_http: int, monkeypatch: Any
) -> None:
    """Resolution happens once; the connection goes to that address with the name in Host."""
    calls: list[str] = []

    def fake_resolve(host: str, port: int) -> list[Any]:
        calls.append(host)
        return [ipaddress.ip_address("127.0.0.1")]

    monkeypatch.setattr(netguard, "resolve", fake_resolve)
    r = post_json(f"http://hooks.test:{local_http}/hook", {"text": "hi"}, allow_private=True)
    assert r.status_code == 200
    assert calls == ["hooks.test"]  # no second lookup by the HTTP client
    assert _Handler.seen[0]["host"] == f"hooks.test:{local_http}"


def test_redirect_to_private_is_not_followed(local_http: int) -> None:
    with pytest.raises(BlockedURL, match="redirect"):
        post_json(f"http://127.0.0.1:{local_http}/redirect", {}, allow_private=True)
    assert [s["path"] for s in _Handler.seen] == ["/redirect"]


def test_reply_size_cap(local_http: int) -> None:
    with pytest.raises(RuntimeError, match="larger"):
        post_json(f"http://127.0.0.1:{local_http}/big", {}, allow_private=True, max_bytes=1000)


def test_timeout() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)  # accepts the connection but never answers
    port = srv.getsockname()[1]
    try:
        with pytest.raises(Exception, match="(?i)timed out|timeout"):
            post_json(f"http://127.0.0.1:{port}/", {}, allow_private=True, timeout=0.3)
    finally:
        srv.close()


def test_alert_webhook_goes_through_guard(store: Any) -> None:
    from tirkit.alerts import Alerts

    a = Alerts(store)
    with pytest.raises(ValueError):
        a.create(
            "x",
            {"metric": "trace_count", "op": ">", "threshold": 0},
            {"type": "webhook", "url": "http://169.254.169.254/latest"},
        )
    # Delivery resolves and checks again; a refused or unresolvable host is recorded, not sent.
    aid = a.create(
        "y",
        {"metric": "trace_count", "op": "<", "threshold": 1},
        {"type": "webhook", "url": "http://localtest.me.example.invalid/hook"},
    )
    alert = next(x for x in a.items() if x["alert_id"] == aid)
    assert a.check(alert) == "firing"
    event = a.events()[0]
    assert event["delivered"] == 0 and "resolve" in event["error"]
