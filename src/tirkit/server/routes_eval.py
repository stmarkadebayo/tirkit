"""Online evaluators, alerts and judge-reliability reports (SPEC FR-10, FR-17, FR-19)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response

from tirkit import jobs
from tirkit.server.app import Ctx, extension


@extension
def register(app: FastAPI, project_ctx: Callable[..., Ctx], key_project: Any, json: Any) -> None:
    from tirkit.alerts import Alerts
    from tirkit.online import Evaluators, spend_today

    @app.get("/api/projects/{project_id}/evaluators")
    def list_evaluators(ctx: Ctx = Depends(project_ctx)) -> Response:
        return json(
            {"evaluators": Evaluators(ctx.store).items(), "spend_today_usd": spend_today(ctx.store)}
        )

    @app.post("/api/projects/{project_id}/evaluators")
    async def create_evaluator(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("member")
        b = await request.json()
        try:
            eid = Evaluators(ctx.store).create(
                b.get("name") or "evaluator",
                b.get("scorer") or {},
                sampling_rate=float(b.get("sampling_rate", 0.1)),
                filter=b.get("filter") or {},
                backfill_s=float(b.get("backfill_s", 0)),
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return json({"evaluator_id": eid}, 201)

    @app.patch("/api/projects/{project_id}/evaluators/{evaluator_id}")
    async def toggle_evaluator(
        evaluator_id: str, request: Request, ctx: Ctx = Depends(project_ctx)
    ) -> Response:
        ctx.require("member")
        b = await request.json()
        Evaluators(ctx.store).set_active(evaluator_id, bool(b.get("active")))
        return json({"ok": True})

    @app.delete("/api/projects/{project_id}/evaluators/{evaluator_id}")
    def delete_evaluator(evaluator_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("member")
        Evaluators(ctx.store).delete(evaluator_id)
        return json({"ok": True})

    @app.get("/api/projects/{project_id}/alerts")
    def list_alerts(ctx: Ctx = Depends(project_ctx)) -> Response:
        a = Alerts(ctx.store)
        return json({"alerts": a.items(), "events": a.events()})

    @app.post("/api/projects/{project_id}/alerts")
    async def create_alert(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("admin")
        b = await request.json()
        try:
            aid = Alerts(ctx.store).create(
                b.get("name") or "alert", b.get("rule") or {}, b.get("channel") or {}
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return json({"alert_id": aid}, 201)

    @app.post("/api/projects/{project_id}/alerts/{alert_id}/check")
    def check_alert(alert_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        a = Alerts(ctx.store)
        found = [x for x in a.items() if x["alert_id"] == alert_id]
        if not found:
            raise HTTPException(404, "no such alert")
        return json({"state": a.check(found[0])})

    @app.delete("/api/projects/{project_id}/alerts/{alert_id}")
    def delete_alert(alert_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("admin")
        Alerts(ctx.store).delete(alert_id)
        return json({"ok": True})

    @app.get("/api/projects/{project_id}/judge-reports")
    def judge_reports(ctx: Ctx = Depends(project_ctx)) -> Response:
        from tirkit.judge import RUBRICS
        from tirkit.reliability import list_reports

        return json(
            {
                "reports": list_reports(ctx.store),
                "rubrics": ctx.store.list_rubrics(),
                "builtin": {k: r.text for k, r in RUBRICS.items()},
            }
        )

    @app.post("/api/projects/{project_id}/judge-reports")
    async def start_judge_report(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("member")
        b = await request.json()
        for key in ("queue_id", "label", "rubric_text", "model"):
            if not b.get(key):
                raise HTTPException(400, f"{key} required")
        return json({"job_id": jobs.enqueue(ctx.store, "judge_report", b)}, 202)
