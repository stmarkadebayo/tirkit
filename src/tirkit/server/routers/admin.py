"""Audit log reading: every event for server admins, one project's events for its admins."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from tirkit import audit
from tirkit.accounts import User
from tirkit.server import models as m
from tirkit.server.deps import Ctx, app_store, json_response, project_ctx, session
from tirkit.store import SQLStore

router = APIRouter(tags=["admin"])


def _page(entries: list[dict[str, object]], limit: int) -> Response:
    nxt = entries[-1]["id"] if len(entries) == limit else None
    return json_response({"entries": entries, "next_before": nxt})


@router.get("/api/admin/audit", response_model=m.AuditPage)
def server_audit(
    project_id: str | None = None,
    action: str | None = Query(None, description='Exact action, or a prefix ending in "."'),
    actor: str | None = None,
    before: int | None = Query(None, description="Only entries with a smaller id"),
    limit: int = Query(100, ge=1, le=1000),
    s: tuple[User, str] = Depends(session),
    store: SQLStore = Depends(app_store),
) -> Response:
    """Every audit event, including sign-ins (server admins only)."""
    if not s[0].is_admin:
        raise HTTPException(403, "requires server admin")
    rows = audit.entries(
        store, project_id=project_id, action=action, actor=actor, before_id=before, limit=limit
    )
    return _page(rows, limit)


@router.get("/api/projects/{project_id}/audit", response_model=m.AuditPage)
def project_audit(
    action: str | None = Query(None, description='Exact action, or a prefix ending in "."'),
    actor: str | None = None,
    before: int | None = Query(None, description="Only entries with a smaller id"),
    limit: int = Query(100, ge=1, le=1000),
    ctx: Ctx = Depends(project_ctx),
    store: SQLStore = Depends(app_store),
) -> Response:
    """This project's audit events (project admins, signed in; API keys cannot read it)."""
    ctx.require("admin")
    if ctx.via == "key":
        raise HTTPException(403, "the audit log needs a signed-in admin")
    rows = audit.entries(
        store, project_id=ctx.project_id, action=action, actor=actor, before_id=before, limit=limit
    )
    return _page(rows, limit)
