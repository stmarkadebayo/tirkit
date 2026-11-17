"""Outbound request guard against server-side request forgery (SSRF).

Every HTTP request the server makes to a URL a user typed in (today: alert webhooks) goes through
:func:`post_json`. It

1. accepts only ``http``/``https`` URLs without credentials, backslashes, spaces or zone ids;
2. refuses ambiguous numeric hosts (``0x7f.1``, ``2130706433``, ``0177.0.0.1``) outright;
3. resolves the host once and refuses it if **any** address is private, loopback, link-local
   (cloud metadata ``169.254.169.254``), CGNAT, multicast, reserved or unspecified, including
   IPv6 forms that embed an IPv4 address (``::ffff:127.0.0.1``, NAT64, 6to4, Teredo);
4. connects to the address it validated (no second lookup, so DNS rebinding cannot swap it), with
   the original host name for ``Host`` and TLS certificate checks;
5. never follows redirects, ignores proxy environment variables, applies a timeout and caps the
   response size.

Self-hosters whose webhook targets live on a private network can set
``TIRKIT_ALLOW_PRIVATE_WEBHOOKS=1``; the other checks still apply.

Operator-configured endpoints (``LLM_BASE_URL``, SMTP) are trusted and not guarded.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

IP = ipaddress.IPv4Address | ipaddress.IPv6Address

ALLOW_PRIVATE_ENV = "TIRKIT_ALLOW_PRIVATE_WEBHOOKS"
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_NUMERIC_LABEL = re.compile(r"0x[0-9a-f]*|\d+", re.IGNORECASE)
_BAD_CHARS = re.compile(r"[\s\\\x00-\x1f\x7f]")


class BlockedURL(ValueError):
    """The URL is not allowed as an outbound target."""


def allow_private_default() -> bool:
    return os.environ.get(ALLOW_PRIVATE_ENV, "") == "1"


def blocked_reason(ip: IP) -> str | None:
    """Why ``ip`` is not a public unicast address, or None when it is."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: ipaddress.IPv4Address | None = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and (ip in _NAT64 or ip in _NAT64_LOCAL):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is None and ip.teredo:
            server, client = ip.teredo
            embedded = client if blocked_reason(client) else server
        if embedded is not None:
            inner = blocked_reason(embedded)
            if inner:
                return f"{inner} (embedded in {ip})"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address (includes cloud metadata)"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "carrier-grade NAT address"
    if ip.is_private:
        return "private address"
    if ip.is_reserved or not ip.is_global:
        return "reserved or non-global address"
    return None


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str  # as written (lower-case), IPv6 without brackets
    port: int
    path: str  # path plus query, at least "/"
    host_header: str
    literal: IP | None


def parse(url: str) -> Target:
    """Syntax checks only (no DNS)."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise BlockedURL("URL is empty or too long")
    if _BAD_CHARS.search(url):
        raise BlockedURL("URL contains spaces, backslashes or control characters")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise BlockedURL("URL must start with http:// or https://")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise BlockedURL("credentials in the URL are not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise BlockedURL("URL has no host")
    if "%" in host:
        raise BlockedURL("IPv6 zone ids are not allowed")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise BlockedURL("invalid port") from exc
    literal: IP | None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
        labels = host.rstrip(".").split(".")
        if all(_NUMERIC_LABEL.fullmatch(label) for label in labels):
            raise BlockedURL(
                f"ambiguous numeric host {host!r}; write the address in full"
            ) from None
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    default = 443 if scheme == "https" else 80
    shown = f"[{host}]" if isinstance(literal, ipaddress.IPv6Address) else host
    host_header = shown if port == default else f"{shown}:{port}"
    return Target(scheme, host, port, path, host_header, literal)


def resolve(host: str, port: int) -> list[IP]:
    """Every address the host resolves to (tests replace this)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[IP] = []
    for info in infos:
        addr = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if addr not in out:
            out.append(addr)
    return out


def check_url(
    url: str, *, allow_private: bool | None = None, dns: bool = True
) -> tuple[Target, list[IP]]:
    """Validate ``url`` and return its target and the addresses to connect to.

    With ``dns=False`` only literal addresses and ``localhost`` names are checked (for quick
    validation when a URL is saved); delivery always resolves.
    """
    target = parse(url)
    allow = allow_private_default() if allow_private is None else allow_private
    if target.literal is not None:
        ips: list[IP] = [target.literal]
    elif not dns:
        name = target.host.rstrip(".")
        if not allow and (name == "localhost" or name.endswith(".localhost")):
            raise BlockedURL("localhost is not allowed")
        return target, []
    else:
        try:
            ips = resolve(target.host, target.port)
        except (OSError, UnicodeError) as exc:
            raise BlockedURL(f"cannot resolve {target.host}: {exc}") from exc
        if not ips:
            raise BlockedURL(f"cannot resolve {target.host}")
    if not allow:
        for ip in ips:
            reason = blocked_reason(ip)
            if reason:
                raise BlockedURL(
                    f"{target.host} resolves to a {reason}; set {ALLOW_PRIVATE_ENV}=1 to allow "
                    "private webhook targets"
                )
    return target, ips


@dataclass
class GuardedResponse:
    status_code: int
    content: bytes


def post_json(
    url: str,
    payload: Any,
    *,
    timeout: float = 10.0,
    max_bytes: int = 64 * 1024,
    allow_private: bool | None = None,
) -> GuardedResponse:
    """POST ``payload`` as JSON to ``url`` through the guard. Raises :class:`BlockedURL` for a
    refused target or redirect and ``RuntimeError`` for an error status or oversized reply."""
    import httpx

    target, ips = check_url(url, allow_private=allow_private)
    ip = ips[0]
    shown = f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)
    pinned = f"{target.scheme}://{shown}:{target.port}{target.path}"
    extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        request = client.build_request(
            "POST",
            pinned,
            json=payload,
            headers={"Host": target.host_header, "User-Agent": "tirkit-webhook"},
            extensions=extensions,
        )
        r = client.send(request, stream=True)
        try:
            if 300 <= r.status_code < 400:
                raise BlockedURL(f"webhook answered {r.status_code}; redirects are not followed")
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise RuntimeError(f"webhook reply larger than {max_bytes} bytes")
            if r.status_code >= 400:
                raise RuntimeError(f"webhook answered {r.status_code}")
            return GuardedResponse(r.status_code, body)
        finally:
            r.close()
