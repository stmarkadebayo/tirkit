"""Request dependencies shared by every router: who is calling, for which project, and the
per-app state (store, accounts, settings, media, a short TTL cache).

Two ways to authenticate:

- **API keys** (SDKs, OTLP exporters, CI): HTTP Basic with the project's public key as the user
  name and the secret key as the password. A key can only touch its own project.
- **Sessions** (web UI): login sets an HttpOnly session cookie plus a CSRF token; every
  state-changing request made with a session must send ``X-CSRF-Token``.

Every project-scoped query goes through a store view bound to that project.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, Response

from tirkit.accounts import ROLES, Accounts, User
from tirkit.media import LocalMediaStore
from tirkit.serialize import dumps
from tirkit.server.settings import Settings
from tirkit.store import SQLStore

SESSION_COOKIE = "tirkit_session"
CSRF_COOKIE = "tirkit_csrf"
_ROLE_RANK = {r: i for i, r in enumerate(reversed(ROLES))}  # viewer 0, member 1, admin 2


@dataclass
class Ctx:
    """Who is calling and for which project."""

    store: SQLStore
    project_id: str
    role: str
    user: User | None = None
    via: str = "session"

    def require(self, role: str) -> None:
        if _ROLE_RANK[self.role] < _ROLE_RANK[role]:
            raise HTTPException(403, f"requires {role} role")

    @property
    def actor(self) -> str:
        """The signed-in user's email, or ``"api"`` for API-key calls."""
        return self.user.email if self.user else "api"


def json_response(data: Any, status: int = 200) -> Response:
    """JSON through :func:`tirkit.serialize.dumps` (handles NaN, bytes and SDK objects)."""
    return Response(dumps(data), status_code=status, media_type="application/json")


# -- per-app state ----------------------------------------------------------------------------


def app_store(request: Request) -> SQLStore:
    store: SQLStore = request.app.state.store
    return store


def app_accounts(request: Request) -> Accounts:
    accounts: Accounts = request.app.state.accounts
    return accounts


def app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def app_media(request: Request) -> LocalMediaStore:
    media: LocalMediaStore = request.app.state.media
    return media


class TTLCache:
    """Short TTL cache for per-project settings and prices on the ingestion hot path."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[float, Any]] = {}

    def get(self, kind: str, pid: str, load: Callable[[], Any], ttl: float = 5.0) -> Any:
        hit = self._data.get((kind, pid))
        now = time.time()
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = load()
        self._data[(kind, pid)] = (now, value)
        return value

    def drop(self, kind: str, pid: str) -> None:
        self._data.pop((kind, pid), None)


def app_cache(request: Request) -> TTLCache:
    cache: TTLCache = request.app.state.cache
    return cache


def project_settings(request: Request, pid: str, fresh: bool = False) -> dict[str, Any]:
    accounts = app_accounts(request)

    def load() -> dict[str, Any]:
        return (accounts.project(pid) or {}).get("settings") or {}

    return load() if fresh else app_cache(request).get("settings", pid, load)


# -- authentication ---------------------------------------------------------------------------


def basic_credentials(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            pk, _, sk = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return None
        return pk, sk
    if header.lower().startswith("bearer ") and ":" in header[7:]:
        pk, _, sk = header[7:].partition(":")
        return pk, sk
    return None


def key_project(request: Request) -> str:
    """The project of the API key on this request, or 401."""
    creds = basic_credentials(request)
    project_id = app_accounts(request).verify_api_key(*creds) if creds else None
    if not project_id:
        raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "Basic"})
    return project_id


def session(request: Request) -> tuple[User, str]:
    """The signed-in user and CSRF token, or 401/403."""
    token = request.cookies.get(SESSION_COOKIE)
    found = app_accounts(request).session_user(token) if token else None
    if not found:
        raise HTTPException(401, "not signed in")
    user, csrf = found
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-csrf-token") != csrf:
            raise HTTPException(403, "missing or invalid CSRF token")
    return user, csrf


def project_ctx(project_id: str, request: Request) -> Ctx:
    """Caller context for ``/api/projects/{project_id}/...`` (API key or session)."""
    accounts = app_accounts(request)
    store = app_store(request)
    creds = basic_credentials(request)
    if creds:
        key_pid = accounts.verify_api_key(*creds)
        if key_pid != project_id:
            raise HTTPException(401 if key_pid is None else 403, "API key not valid here")
        return Ctx(store.for_project(project_id), project_id, "member", via="key")
    user, _ = session(request)
    role = accounts.role(user, project_id)
    if role is None:
        raise HTTPException(404, "project not found")
    return Ctx(store.for_project(project_id), project_id, role, user)


def set_session_cookies(request: Request, resp: Response, token: str, csrf: str) -> None:
    opts = {"secure": app_settings(request).secure_cookies, "samesite": "lax", "path": "/"}
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, max_age=14 * 86400, **opts)  # type: ignore[arg-type]
    resp.set_cookie(CSRF_COOKIE, csrf, httponly=False, max_age=14 * 86400, **opts)  # type: ignore[arg-type]


async def read_body(request: Request, limit: int) -> bytes:
    """The raw body, refusing anything over ``limit`` bytes with 413."""
    size = int(request.headers.get("content-length") or 0)
    if size > limit:
        raise HTTPException(413, "request too large")
    body = await request.body()
    if len(body) > limit:
        raise HTTPException(413, "request too large")
    return body


def query_filters(request: Request) -> dict[str, Any]:
    """Trace and span filters from the query string."""
    q = request.query_params
    where: dict[str, Any] = {}
    for key in ("name", "user_id", "session_id", "release", "tag", "search", "model", "kind"):
        if q.get(key):
            where[key] = q[key]
    for key in ("start_after", "start_before", "min_cost", "min_duration_ms"):
        if q.get(key):
            where[key] = float(q[key])
    if q.get("error") in ("true", "false"):
        where["error"] = q["error"] == "true"
    return where
