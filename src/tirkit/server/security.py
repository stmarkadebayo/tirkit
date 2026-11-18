"""HTTP security for the server: one middleware for headers and CORS, the client address behind
reverse proxies, login throttling and per-key ingestion limits.

See ``docs/site/security.md`` for the defaults and how to change them.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from tirkit import audit
from tirkit.accounts import User
from tirkit.ratelimit import (
    Backend,
    DatabaseBackend,
    Decision,
    MemoryBackend,
    ProgressiveDelay,
    RateLimiter,
    Rule,
)
from tirkit.server.deps import Ctx, app_store, basic_credentials
from tirkit.server.settings import Settings

log = logging.getLogger("tirkit.security")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# The UI is a static bundle: scripts and styles from our own origin only, no inline script.
# Inline *style attributes* set by React go through the CSSOM and need no 'unsafe-inline'.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
# Swagger UI at /api/docs loads its bundle from jsDelivr and configures itself inline.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
)
HSTS = "max-age=31536000; includeSubDomains"

# Routes that browsers on other origins may call when TIRKIT_CORS_ORIGINS allows it: span, OTLP,
# media and score ingestion with an API key. Nothing that uses the session cookie.
CORS_PREFIXES = ("/api/v1/", "/v1/traces")
CORS_METHODS = "GET, POST, PUT, OPTIONS"
CORS_HEADERS = "authorization, content-type, content-encoding"


def _is_https(scope: Scope, trusted_proxies: int) -> bool:
    if scope.get("scheme") == "https":
        return True
    if trusted_proxies > 0:
        for name, value in scope.get("headers") or []:
            if name == b"x-forwarded-proto":
                return value.decode("latin-1").split(",")[-1].strip().lower() == "https"
    return False


class SecurityMiddleware:
    """Adds security headers to every response and answers CORS for the ingestion routes.

    A plain ASGI middleware (no request object, no body buffering) so it costs microseconds.
    Headers a route already set (for example the stricter ``sandbox`` CSP on media) are kept.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.trusted_proxies = settings.trusted_proxies
        self.origins = set(settings.cors_origins)

    def _allowed_origin(self, origin: str | None, path: str) -> str | None:
        if not origin or not self.origins or not path.startswith(CORS_PREFIXES):
            return None
        if "*" in self.origins:
            return "*"
        return origin if origin.rstrip("/") in self.origins else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        headers = {k: v for k, v in scope.get("headers") or []}
        origin = headers.get(b"origin", b"").decode("latin-1") or None
        allow = self._allowed_origin(origin, path)
        if scope["method"] == "OPTIONS" and allow and b"access-control-request-method" in headers:
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": self._extra([], scope, allow, preflight=True),
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = self._extra(list(message.get("headers") or []), scope, allow)
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _extra(
        self,
        raw: list[tuple[bytes, bytes]],
        scope: Scope,
        allow: str | None,
        preflight: bool = False,
    ) -> list[tuple[bytes, bytes]]:
        present = {k.lower() for k, _ in raw}
        path: str = scope.get("path", "")
        add: list[tuple[str, str]] = [
            ("content-security-policy", DOCS_CSP if path.startswith("/api/docs") else CSP),
            ("x-content-type-options", "nosniff"),
            ("referrer-policy", "no-referrer"),
            ("x-frame-options", "DENY"),
            ("cross-origin-opener-policy", "same-origin"),
        ]
        if _is_https(scope, self.trusted_proxies):
            add.append(("strict-transport-security", HSTS))
        if allow:
            add.append(("access-control-allow-origin", allow))
            add.append(("vary", "Origin"))
            if preflight:
                add += [
                    ("access-control-allow-methods", CORS_METHODS),
                    ("access-control-allow-headers", CORS_HEADERS),
                    ("access-control-max-age", "600"),
                ]
        for name, value in add:
            key = name.encode()
            if key not in present:
                raw.append((key, value.encode()))
        return raw


# -- client address ----------------------------------------------------------------------------


def client_ip(request: Request, trusted_proxies: int | None = None) -> str:
    """The caller's address. With ``trusted_proxies = N`` the N-th address from the right of
    ``X-Forwarded-For`` (each trusted proxy appends the address it saw); with 0 the socket address.
    """
    if trusted_proxies is None:
        trusted_proxies = request.app.state.settings.trusted_proxies
    peer = request.client.host if request.client else "unknown"
    if trusted_proxies <= 0:
        return peer
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    if not hops:
        return peer
    return hops[-trusted_proxies] if len(hops) >= trusted_proxies else hops[0]


# -- limits ------------------------------------------------------------------------------------


@dataclass
class Limits:
    login_account: ProgressiveDelay
    login_ip: ProgressiveDelay
    login_global: RateLimiter
    ingest: RateLimiter | None


def _sqlite_too_old(store: Any) -> bool:
    return store.dialect.name == "sqlite" and sqlite3.sqlite_version_info < (3, 35, 0)


def build_limits(settings: Settings, store: Any) -> Limits:
    backend: Backend = (
        MemoryBackend() if settings.rate_limit_backend == "memory" else DatabaseBackend(store)
    )
    if isinstance(backend, DatabaseBackend) and _sqlite_too_old(store):
        log.warning(
            "SQLite %s lacks RETURNING (3.35+): rate limits are counted per process",
            sqlite3.sqlite_version,
        )
        backend = MemoryBackend()

    def delay(name: str, free: int) -> ProgressiveDelay:
        return ProgressiveDelay(
            name,
            free=free,
            base_s=1.0,
            cap_s=settings.login_max_delay_s,
            reset_after_s=settings.login_reset_after_s,
            backend=backend,
        )

    rate = settings.ingest_spans_per_s
    ingest = None
    if rate > 0:
        burst = settings.ingest_burst if settings.ingest_burst is not None else rate * 10
        ingest = RateLimiter("ingest", Rule(rate, 1.0, burst=burst), backend)
    g = settings.login_global_per_s
    return Limits(
        login_account=delay("login:acct", settings.login_free_per_account),
        login_ip=delay("login:ip", settings.login_free_per_ip),
        login_global=RateLimiter("login:all", Rule(g, 1.0, burst=g * 10), backend),
        ingest=ingest,
    )


def _limits(request: Request) -> Limits:
    limits: Limits = request.app.state.limits
    return limits


def too_many(decision: Decision, detail: str) -> HTTPException:
    return HTTPException(
        429, detail, headers={"Retry-After": str(max(1, math.ceil(decision.retry_after)))}
    )


LOGIN_THROTTLED = "too many sign-in attempts; try again later"


def check_login(request: Request, email: str) -> None:
    """Count a sign-in attempt; 429 with ``Retry-After`` when any limit says wait.

    The same message for every limit, so the response does not reveal whether an account exists.
    Checked in order global, IP, account: an attempt refused by an earlier limit is not counted
    against the later ones.
    """
    lim = _limits(request)
    ip = client_ip(request)
    for decision in (
        lim.login_global.hit("all"),
        lim.login_ip.attempt(ip),
        lim.login_account.attempt(email.strip().lower()),
    ):
        if not decision.allowed:
            raise too_many(decision, LOGIN_THROTTLED)


def login_succeeded(request: Request, email: str) -> None:
    """A correct password clears the account's count and takes back the IP's attempt."""
    lim = _limits(request)
    lim.login_account.reset(email.strip().lower())
    lim.login_ip.refund(client_ip(request))


def enforce_ingest_limit(request: Request, public_key: str | None, n_spans: int) -> None:
    """Spend ``n_spans`` tokens from the key's bucket, or 429 with ``Retry-After``."""
    lim = _limits(request).ingest
    if lim is None or not public_key or n_spans <= 0:
        return
    # A batch bigger than the burst could never pass; it is charged as a full bucket instead.
    decision = lim.hit(public_key, cost=min(float(n_spans), lim.rule.capacity))
    if not decision.allowed:
        raise too_many(decision, "ingest rate limit exceeded for this API key; retry later")


# -- audit -------------------------------------------------------------------------------------


def audit_event(
    request: Request,
    action: str,
    *,
    ctx: Ctx | None = None,
    user: User | None = None,
    actor: str | None = None,
    project_id: str | None = None,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append an audit event for this request (actor, address and time filled in)."""
    if ctx is not None:
        user = user or ctx.user
        project_id = project_id or ctx.project_id
        if ctx.via == "key" and actor is None:
            creds = basic_credentials(request)
            actor = f"api:{creds[0]}" if creds else "api"
    audit.record(
        app_store(request),
        action,
        actor=actor or (user.email if user else None),
        actor_id=user.user_id if user else None,
        ip=client_ip(request),
        project_id=project_id,
        target=target,
        detail=detail,
    )
