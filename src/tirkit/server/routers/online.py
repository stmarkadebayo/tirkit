"""Online evaluators, alerts and judge-reliability reports (SPEC FR-10, FR-17, FR-19)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from tirkit import jobs
from tirkit.alerts import Alerts
from tirkit.online import Evaluators, spend_today
from tirkit.server import models as m
from tirkit.server.deps import Ctx, json_response, project_ctx
from tirkit.server.security import audit_event

router = APIRouter(tags=["online"])


@router.get("/api/projects/{project_id}/evaluators", response_model=m.EvaluatorList)
def list_evaluators(ctx: Ctx = Depends(project_ctx)) -> Response:
    return json_response(
        {"evaluators": Evaluators(ctx.store).items(), "spend_today_usd": spend_today(ctx.store)}
    )


@router.post(
    "/api/projects/{project_id}/evaluators", response_model=m.EvaluatorCreated, status_code=201
)
def create_evaluator(body: m.CreateEvaluatorRequest, ctx: Ctx = Depends(project_ctx)) -> Response:
    """Score a sample of new traces as they arrive."""
    ctx.require("member")
    try:
        eid = Evaluators(ctx.store).create(
            body.name or "evaluator",
            body.scorer or {},
            sampling_rate=body.sampling_rate,
            filter=body.filter or {},
            backfill_s=body.backfill_s,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return json_response({"evaluator_id": eid}, 201)


@router.patch("/api/projects/{project_id}/evaluators/{evaluator_id}", response_model=m.Ok)
def toggle_evaluator(
    evaluator_id: str, body: m.ToggleEvaluatorRequest, ctx: Ctx = Depends(project_ctx)
) -> Response:
    ctx.require("member")
    Evaluators(ctx.store).set_active(evaluator_id, body.active)
    return json_response({"ok": True})


@router.delete("/api/projects/{project_id}/evaluators/{evaluator_id}", response_model=m.Ok)
def delete_evaluator(
    evaluator_id: str, request: Request, ctx: Ctx = Depends(project_ctx)
) -> Response:
    ctx.require("member")
    Evaluators(ctx.store).delete(evaluator_id)
    audit_event(request, "evaluator.delete", ctx=ctx, target=evaluator_id)
    return json_response({"ok": True})


@router.get("/api/projects/{project_id}/alerts", response_model=m.AlertList)
def list_alerts(ctx: Ctx = Depends(project_ctx)) -> Response:
    a = Alerts(ctx.store)
    return json_response({"alerts": a.items(), "events": a.events()})


@router.post("/api/projects/{project_id}/alerts", response_model=m.AlertCreated, status_code=201)
def create_alert(
    body: m.CreateAlertRequest, request: Request, ctx: Ctx = Depends(project_ctx)
) -> Response:
    ctx.require("admin")
    try:
        aid = Alerts(ctx.store).create(body.name or "alert", body.rule or {}, body.channel or {})
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    channel = body.channel or {}
    audit_event(
        request,
        "alert.create",
        ctx=ctx,
        target=aid,
        detail={"channel": channel.get("type"), "url": channel.get("url"), "to": channel.get("to")},
    )
    return json_response({"alert_id": aid}, 201)


@router.post("/api/projects/{project_id}/alerts/{alert_id}/check", response_model=m.AlertState)
def check_alert(alert_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
    """Evaluate the alert now (and notify if it fires)."""
    a = Alerts(ctx.store)
    found = [x for x in a.items() if x["alert_id"] == alert_id]
    if not found:
        raise HTTPException(404, "no such alert")
    return json_response({"state": a.check(found[0])})


@router.delete("/api/projects/{project_id}/alerts/{alert_id}", response_model=m.Ok)
def delete_alert(alert_id: str, request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
    ctx.require("admin")
    Alerts(ctx.store).delete(alert_id)
    audit_event(request, "alert.delete", ctx=ctx, target=alert_id)
    return json_response({"ok": True})


@router.get("/api/projects/{project_id}/judge-reports", response_model=m.JudgeReports)
def judge_reports(ctx: Ctx = Depends(project_ctx)) -> Response:
    from tirkit.judge import RUBRICS
    from tirkit.reliability import list_reports

    return json_response(
        {
            "reports": list_reports(ctx.store),
            "rubrics": ctx.store.list_rubrics(),
            "builtin": {k: r.text for k, r in RUBRICS.items()},
        }
    )


@router.post(
    "/api/projects/{project_id}/judge-reports", response_model=m.JobQueued, status_code=202
)
def start_judge_report(body: m.JudgeReportRequest, ctx: Ctx = Depends(project_ctx)) -> Response:
    """Queue a judge run over a queue's human labels; the report measures agreement."""
    ctx.require("member")
    b = body.model_dump(exclude_unset=True)
    for key in ("queue_id", "label", "rubric_text", "model"):
        if not b.get(key):
            raise HTTPException(400, f"{key} required")
    return json_response({"job_id": jobs.enqueue(ctx.store, "judge_report", b)}, 202)
