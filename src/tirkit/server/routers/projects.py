"""Projects, members, API keys, project settings and price overrides."""

from __future__ import annotations

import re

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from tirkit import pricing
from tirkit.accounts import Accounts, User
from tirkit.server import models as m
from tirkit.server.deps import (
    Ctx,
    app_accounts,
    app_cache,
    json_response,
    key_project,
    project_ctx,
    project_settings,
    session,
)

router = APIRouter(tags=["projects"])

SETTINGS_KEYS = {"redact", "redact_patterns", "retention_days", "judge_budget_usd_per_day", "name"}


@router.post("/api/projects", response_model=m.ProjectCreated, status_code=201)
def create_project(
    body: m.CreateProjectRequest,
    s: tuple[User, str] = Depends(session),
    accounts: Accounts = Depends(app_accounts),
) -> Response:
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    return json_response({"project_id": accounts.create_project(name, s[0].user_id)}, 201)


@router.get("/api/projects/{project_id}", response_model=m.Project)
def get_project(
    ctx: Ctx = Depends(project_ctx), accounts: Accounts = Depends(app_accounts)
) -> Response:
    return json_response({**(accounts.project(ctx.project_id) or {}), "role": ctx.role})


@router.put("/api/projects/{project_id}/settings", response_model=m.ProjectSettings)
def put_settings(
    body: m.SettingsUpdate,
    request: Request,
    ctx: Ctx = Depends(project_ctx),
    accounts: Accounts = Depends(app_accounts),
) -> Response:
    ctx.require("admin")
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in SETTINGS_KEYS}
    settings_ = {**project_settings(request, ctx.project_id, fresh=True), **updates}
    for pat in settings_.get("redact_patterns") or []:
        try:
            re.compile(pat["pattern"])
        except (re.error, KeyError, TypeError) as exc:
            raise HTTPException(400, f"bad redaction pattern: {exc}") from exc
    accounts.update_settings(ctx.project_id, settings_)
    app_cache(request).drop("settings", ctx.project_id)
    return json_response(settings_)


@router.get("/api/projects/{project_id}/members", response_model=list[m.Member])
def list_members(
    ctx: Ctx = Depends(project_ctx), accounts: Accounts = Depends(app_accounts)
) -> Response:
    return json_response(accounts.members(ctx.project_id))


@router.post("/api/projects/{project_id}/members", response_model=m.UserCreated, status_code=201)
def add_member(
    body: m.AddMemberRequest,
    ctx: Ctx = Depends(project_ctx),
    accounts: Accounts = Depends(app_accounts),
) -> Response:
    """Add an existing user by email, or create one when ``password`` is given."""
    ctx.require("admin")
    user = accounts.find_user(body.email)
    if user is None:
        if not body.password:
            raise HTTPException(404, "no such user; pass a password to create one")
        try:
            user = accounts.create_user(body.email, body.password, name=body.name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        accounts.set_role(user.user_id, ctx.project_id, body.role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return json_response({"user_id": user.user_id}, 201)


@router.delete("/api/projects/{project_id}/members/{user_id}", response_model=m.Ok)
def remove_member(
    user_id: str, ctx: Ctx = Depends(project_ctx), accounts: Accounts = Depends(app_accounts)
) -> Response:
    ctx.require("admin")
    accounts.remove_member(user_id, ctx.project_id)
    return json_response({"ok": True})


@router.get("/api/projects/{project_id}/keys", response_model=list[m.ApiKeyInfo])
def list_keys(
    ctx: Ctx = Depends(project_ctx), accounts: Accounts = Depends(app_accounts)
) -> Response:
    ctx.require("admin")
    return json_response(accounts.list_api_keys(ctx.project_id))


@router.post("/api/projects/{project_id}/keys", response_model=m.ApiKeyCreated, status_code=201)
def create_key(
    body: m.CreateKeyRequest | None = None,
    ctx: Ctx = Depends(project_ctx),
    accounts: Accounts = Depends(app_accounts),
) -> Response:
    ctx.require("admin")
    pk, sk = accounts.create_api_key(ctx.project_id, body.name if body else None)
    return json_response({"public_key": pk, "secret_key": sk}, 201)


@router.delete("/api/projects/{project_id}/keys/{public_key}", response_model=m.Ok)
def delete_key(
    public_key: str, ctx: Ctx = Depends(project_ctx), accounts: Accounts = Depends(app_accounts)
) -> Response:
    ctx.require("admin")
    accounts.delete_api_key(ctx.project_id, public_key)
    return json_response({"ok": True})


@router.get("/api/projects/{project_id}/prices", response_model=m.Prices)
def get_prices(ctx: Ctx = Depends(project_ctx)) -> Response:
    """Per-model price overrides: model -> unit -> USD per unit."""
    return json_response(ctx.store.get_prices())


@router.put("/api/projects/{project_id}/prices/{model:path}", response_model=m.PriceSet)
def put_price(
    model: str,
    request: Request,
    body: dict[str, float] | None = Body(None, description="unit -> USD; null removes"),
    ctx: Ctx = Depends(project_ctx),
) -> Response:
    ctx.require("admin")
    prices = {k: float(v) for k, v in body.items()} if body else None
    if prices and set(prices) - set(pricing.UNITS):
        raise HTTPException(400, f"unknown units: {sorted(set(prices) - set(pricing.UNITS))}")
    ctx.store.set_price(model, prices)
    app_cache(request).drop("prices", ctx.project_id)
    return json_response({"model": model, "prices": prices})


@router.get("/api/v1/project", response_model=m.KeyProject)
def key_whoami(
    pid: str = Depends(key_project), accounts: Accounts = Depends(app_accounts)
) -> Response:
    """The project an API key belongs to."""
    proj = accounts.project(pid) or {}
    return json_response({"project_id": pid, "name": proj.get("name")})
