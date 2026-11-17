"""Health, first-run setup and session login."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from tirkit.accounts import Accounts, User
from tirkit.server import models as m
from tirkit.server.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    app_accounts,
    app_store,
    json_response,
    session,
    set_session_cookies,
)
from tirkit.store import SQLStore

router = APIRouter(tags=["system"])


@router.get("/api/health", response_model=m.Health)
def health(store: SQLStore = Depends(app_store)) -> Response:
    store.execute("SELECT 1")
    return json_response({"ok": True, "dialect": store.dialect.name})


@router.get("/api/setup", response_model=m.SetupStatus)
def setup_status(accounts: Accounts = Depends(app_accounts)) -> Response:
    return json_response({"needs_setup": accounts.count_users() == 0})


@router.post("/api/setup", response_model=m.SetupResult)
def setup(
    body: m.SetupRequest, request: Request, accounts: Accounts = Depends(app_accounts)
) -> Response:
    """Create the first admin and project. Refused (409) once any user exists."""
    if accounts.count_users() > 0:
        raise HTTPException(409, "already set up")
    try:
        user = accounts.create_user(body.email, body.password, name=body.name, is_admin=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    pid = accounts.create_project(body.project_name or "Default", user.user_id)
    token, csrf = accounts.create_session(user.user_id)
    resp = json_response({"user": asdict(user), "project_id": pid})
    set_session_cookies(request, resp, token, csrf)
    return resp


@router.post("/api/auth/login", response_model=m.LoginResult)
def login(
    body: m.LoginRequest, request: Request, accounts: Accounts = Depends(app_accounts)
) -> Response:
    user = accounts.authenticate(body.email, body.password)
    if not user:
        raise HTTPException(401, "wrong email or password")
    token, csrf = accounts.create_session(user.user_id)
    resp = json_response({"user": asdict(user)})
    set_session_cookies(request, resp, token, csrf)
    return resp


@router.post("/api/auth/logout", response_model=m.Ok)
def logout(
    request: Request,
    s: tuple[User, str] = Depends(session),
    accounts: Accounts = Depends(app_accounts),
) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        accounts.delete_session(token)
    resp = json_response({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")
    return resp


@router.get("/api/auth/me", response_model=m.Me)
def me(
    s: tuple[User, str] = Depends(session), accounts: Accounts = Depends(app_accounts)
) -> Response:
    user, csrf = s
    return json_response(
        {"user": asdict(user), "csrf": csrf, "projects": accounts.projects_for(user)}
    )
