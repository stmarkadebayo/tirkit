"""Score configs and annotation queues (SPEC FR-18)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response

from tirkit.annotation import Annotation
from tirkit.serialize import dumps
from tirkit.server.app import Ctx, extension


@extension
def register(app: FastAPI, project_ctx: Callable[..., Ctx], key_project: Any, json: Any) -> None:
    def ann(ctx: Ctx) -> Annotation:
        return Annotation(ctx.store)

    def reviewer(ctx: Ctx) -> str:
        return ctx.user.email if ctx.user else "api"

    @app.get("/api/projects/{project_id}/score-configs")
    def list_configs(ctx: Ctx = Depends(project_ctx)) -> Response:
        return json(ann(ctx).configs())

    @app.put("/api/projects/{project_id}/score-configs/{name}")
    async def put_config(name: str, request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("member")
        b = await request.json()
        try:
            ann(ctx).save_config(
                name,
                b.get("data_type", "numeric"),
                min_value=b.get("min_value"),
                max_value=b.get("max_value"),
                categories=b.get("categories"),
                description=b.get("description"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return json({"ok": True})

    @app.get("/api/projects/{project_id}/queues")
    def list_queues(ctx: Ctx = Depends(project_ctx)) -> Response:
        return json(ann(ctx).queues())

    @app.post("/api/projects/{project_id}/queues")
    async def create_queue(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("member")
        b = await request.json()
        try:
            qid = ann(ctx).create_queue(
                b.get("name") or "Queue", b.get("score_names") or [], b.get("description")
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return json({"queue_id": qid}, 201)

    @app.post("/api/projects/{project_id}/queues/{queue_id}/items")
    async def add_items(
        queue_id: str, request: Request, ctx: Ctx = Depends(project_ctx)
    ) -> Response:
        """Add traces by id, or every trace matching a filter.

        Body: ``{"trace_ids": [...]}`` or ``{"filter": {...}, "limit": n}``.
        """
        ctx.require("member")
        b = await request.json()
        ids = list(b.get("trace_ids") or [])
        if b.get("filter") is not None:
            where = {k: v for k, v in b["filter"].items() if v not in (None, "")}
            ids += [
                t.trace_id for t in ctx.store.query_traces(where, limit=int(b.get("limit", 100)))
            ]
        try:
            n = ann(ctx).add_items(queue_id, ids)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return json({"added": n})

    @app.get("/api/projects/{project_id}/queues/{queue_id}/next")
    def next_item(queue_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        a = ann(ctx)
        try:
            q = a.queue(queue_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        item = a.next_item(queue_id, reviewer(ctx))
        configs = [c for c in a.configs() if c["name"] in q["score_names"]]
        if item is None:
            return json({"queue": q, "item": None, "configs": configs})
        trace = ctx.store.get_trace(item["trace_id"])
        spans = ctx.store.get_spans(item["trace_id"])
        return Response(
            dumps(
                {
                    "queue": q,
                    "configs": configs,
                    "item": item,
                    "trace": asdict(trace) if trace else None,
                    "spans": [s.to_dict() for s in spans],
                }
            ),
            media_type="application/json",
        )

    @app.post("/api/projects/{project_id}/queues/{queue_id}/items/{item_id}/complete")
    async def complete(
        queue_id: str, item_id: str, request: Request, ctx: Ctx = Depends(project_ctx)
    ) -> Response:
        ctx.require("member")
        b = await request.json()
        try:
            ids = ann(ctx).complete(
                queue_id, item_id, reviewer(ctx), b.get("scores") or {}, b.get("comment")
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return json({"score_ids": ids})

    @app.post("/api/projects/{project_id}/queues/{queue_id}/items/{item_id}/skip")
    def skip(queue_id: str, item_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("member")
        ann(ctx).skip(queue_id, item_id, reviewer(ctx))
        return json({"ok": True})

    @app.get("/api/projects/{project_id}/queues/{queue_id}/labels.jsonl")
    def labels(queue_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        try:
            rows = ann(ctx).labels(queue_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(
            "".join(dumps(r) + "\n" for r in rows),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename=labels-{queue_id}.jsonl"},
        )
