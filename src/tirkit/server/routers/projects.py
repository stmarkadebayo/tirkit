"""Projects, members, API keys, project settings and price overrides."""

from __future__ import annotations

import re

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from tirkit import pricing
from tirkit.accounts import Accounts, User
from tirkit.redact import DEFAULT_MODE, MODES, mode_from_settings
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
from tirkit.server.security import audit_event

router = APIRouter(tags=["projects"])

SETTINGS_KEYS = {
    "redact",
    "redact_mode",
    "redact_patterns",
    "retention_days",
    "judge_budget_usd_per_day",
    "name",
}


@router.post("/api/projects", response_model=m.ProjectCreated, status_code=201)
def create_project(
    body: m.CreateProjectRequest,
    request: Request,
    s: tuple[User, str] = Depends(session),
    accounts: Accounts = Depends(app_accounts),
) -> Response:
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    pid = accounts.create_project(name, s[0].user_id)
    audit_event(request, "project.create", user=s[0], project_id=pid, detail={"name": name})
    return json_response({"project_id": pid}, 201)


@router.get("/api/projects/{project_id}", response_model=m.Project)
def get_project(
    ctx: Ctx = Depends(project_ctx), accounts: Accounts = Depends(app_accounts)
) -> Response:
    proj = accounts.project(ctx.project_id) or {}
    settings_ = proj.get("settings") or {}
    settings_["redact_mode"] = mode_from_settings(settings_)
    return json_response({**proj, "role": ctx.role, "redactions": ctx.store.redaction_count()})


@router.put("/api/projects/{project_id}/settings", response_model=m.ProjectSettings)
def put_settings(
    body: m.SettingsUpdate,
    request: Request,
    ctx: Ctx = Depends(project_ctx),
    accounts: Accounts = Depends(app_accounts),
) -> Response:
    ctx.require("admin")
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in SETTINGS_KEYS}
    if "redact_mode" not in updates and "redact" in updates:  # the older on/off switch
        updates["redact_mode"] = "strict" if updates["redact"] else "off"
    if "redact_mode" in updates and updates["redact_mode"] is None:
        updates["redact_mode"] = DEFAULT_MODE  # null clears it back to the default
    if updates.get("redact_mode") is not None and updates["redact_mode"] not in MODES:
        raise HTTPException(400, f"redact_mode must be one of {list(MODES)}")
    if "redact_mode" in updates:
        updates["redact"] = updates["redact_mode"] == "strict"
    settings_ = {**project_settings(request, ctx.project_id, fresh=True), **updates}
    for pat in settings_.get("redact_patterns") or []:
        try:
            re.compile(pat["pattern"])
        except (re.error, KeyError, TypeError) as exc:
            raise HTTPException(400, f"bad redaction pattern: {exc}") from exc
    accounts.update_settings(ctx.project_id, settings_)
    app_cache(request).drop("settings", ctx.project_id)
    audit_event(request, "settings.update", ctx=ctx, detail={"changed": sorted(updates)})
    return json_response({**settings_, "redact_mode": mode_from_settings(settings_)})


@router.get("/api/projects/{project_id}/members", response_model=list[m.Member])
def list_members(
    ctx: Ctx = Depends(project_ctx), accounts: Accounts = Depends(app_accounts)
) -> Response:
    return json_response(accounts.members(ctx.project_id))


@router.post("/api/projects/{project_id}/members", response_model=m.UserCreated, status_code=201)
def add_member(
    body: m.AddMemberRequest,
    request: Request,
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
    audit_event(request, "member.add", ctx=ctx, target=user.email, detail={"role": body.role})
    return json_response({"user_id": user.user_id}, 201)


@router.delete("/api/projects/{project_id}/members/{user_id}", response_model=m.Ok)
def remove_member(
    user_id: str,
    request: Request,
    ctx: Ctx = Depends(project_ctx),
    accounts: Accounts = Depends(app_accounts),
) -> Response:
    ctx.require("admin")
    gone = accounts.get_user(user_id)
    accounts.remove_member(user_id, ctx.project_id)
    audit_event(request, "member.remove", ctx=ctx, target=gone.email if gone else user_id)
    return json_response({"ok": True})


@router.get("/api/projects/{project_id}/keys", response_model=list[m.ApiKeyInfo])
def list_keys(
    ctx: Ctx = Depends(project_ctx), accounts: Accounts = Depends(app_accounts)
) -> Response:
    ctx.require("admin")
    return json_response(accounts.list_api_keys(ctx.project_id))


@router.post("/api/projects/{project_id}/keys", response_model=m.ApiKeyCreated, status_code=201)
def create_key(
    request: Request,
    body: m.CreateKeyRequest | None = None,
    ctx: Ctx = Depends(project_ctx),
    accounts: Accounts = Depends(app_accounts),
) -> Response:
    ctx.require("admin")
    name = body.name if body else None
    expires_at = body.expires_at if body else None
    try:
        pk, sk = accounts.create_api_key(ctx.project_id, name, expires_at=expires_at)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit_event(
        request, "key.create", ctx=ctx, target=pk, detail={"name": name, "expires_at": expires_at}
    )
    return json_response({"public_key": pk, "secret_key": sk, "expires_at": expires_at}, 201)


@router.post(
    "/api/projects/{project_id}/keys/{public_key}/rotate",
    response_model=m.ApiKeyRotated,
    status_code=201,
)
def rotate_key(
    public_key: str,
    request: Request,
    body: m.RotateKeyRequest | None = None,
    ctx: Ctx = Depends(project_ctx),
    accounts: Accounts = Depends(app_accounts),
) -> Response:
    """Issue a replacement key. The old key keeps working for ``overlap_s`` seconds (default one
    day, 0 to revoke it now) so running apps can switch over without dropping traces."""
    ctx.require("admin")
    overlap = body.overlap_s if body and body.overlap_s is not None else 86400.0
    try:
        pk, sk, old_expiry = accounts.rotate_api_key(ctx.project_id, public_key, overlap)
    except KeyError as exc:
        raise HTTPException(404, "no such key") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit_event(
        request,
        "key.rotate",
        ctx=ctx,
        target=public_key,
        detail={"new_key": pk, "old_key_expires_at": old_expiry},
    )
    return json_response(
        {
            "public_key": pk,
            "secret_key": sk,
            "previous_key": public_key,
            "previous_expires_at": old_expiry,
        },
        201,
    )


@router.delete("/api/projects/{project_id}/keys/{public_key}", response_model=m.Ok)
def delete_key(
    public_key: str,
    request: Request,
    ctx: Ctx = Depends(project_ctx),
    accounts: Accounts = Depends(app_accounts),
) -> Response:
    ctx.require("admin")
    accounts.delete_api_key(ctx.project_id, public_key)
    audit_event(request, "key.delete", ctx=ctx, target=public_key)
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
    audit_event(request, "price.set", ctx=ctx, target=model, detail={"prices": prices})
    return json_response({"model": model, "prices": prices})


@router.get("/api/v1/project", response_model=m.KeyProject)
def key_whoami(
    pid: str = Depends(key_project), accounts: Accounts = Depends(app_accounts)
) -> Response:
    """The project an API key belongs to."""
    proj = accounts.project(pid) or {}
    return json_response({"project_id": pid, "name": proj.get("name")})
