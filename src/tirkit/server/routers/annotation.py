"""Score configs and annotation queues (SPEC FR-18)."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Response

from tirkit.annotation import Annotation
from tirkit.serialize import dumps
from tirkit.server import models as m
from tirkit.server.deps import Ctx, json_response, project_ctx

router = APIRouter(tags=["annotation"])


@router.get("/api/projects/{project_id}/score-configs", response_model=list[m.ScoreConfig])
def list_configs(ctx: Ctx = Depends(project_ctx)) -> Response:
    return json_response(Annotation(ctx.store).configs())


@router.put("/api/projects/{project_id}/score-configs/{name}", response_model=m.Ok)
def put_config(name: str, body: m.ScoreConfigRequest, ctx: Ctx = Depends(project_ctx)) -> Response:
    ctx.require("member")
    try:
        Annotation(ctx.store).save_config(
            name,
            body.data_type,
            min_value=body.min_value,
            max_value=body.max_value,
            categories=body.categories,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return json_response({"ok": True})


@router.get("/api/projects/{project_id}/queues", response_model=list[m.Queue])
def list_queues(ctx: Ctx = Depends(project_ctx)) -> Response:
    return json_response(Annotation(ctx.store).queues())


@router.post("/api/projects/{project_id}/queues", response_model=m.QueueCreated, status_code=201)
def create_queue(body: m.CreateQueueRequest, ctx: Ctx = Depends(project_ctx)) -> Response:
    ctx.require("member")
    try:
        qid = Annotation(ctx.store).create_queue(
            body.name or "Queue", body.score_names or [], body.description
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return json_response({"queue_id": qid}, 201)


@router.post("/api/projects/{project_id}/queues/{queue_id}/items", response_model=m.ItemsAdded)
def add_items(queue_id: str, body: m.AddItemsRequest, ctx: Ctx = Depends(project_ctx)) -> Response:
    """Add traces by id, or every trace matching a filter."""
    ctx.require("member")
    ids = list(body.trace_ids or [])
    if body.filter is not None:
        where = {k: v for k, v in body.filter.items() if v not in (None, "")}
        ids += [t.trace_id for t in ctx.store.query_traces(where, limit=body.limit)]
    try:
        n = Annotation(ctx.store).add_items(queue_id, ids)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return json_response({"added": n})


@router.get("/api/projects/{project_id}/queues/{queue_id}/next", response_model=m.NextItem)
def next_item(queue_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
    """The next item for this reviewer, with its trace and spans (``item`` is null when done)."""
    a = Annotation(ctx.store)
    try:
        q = a.queue(queue_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    item = a.next_item(queue_id, ctx.actor)
    configs = [c for c in a.configs() if c["name"] in q["score_names"]]
    if item is None:
        return json_response({"queue": q, "item": None, "configs": configs})
    trace = ctx.store.get_trace(item["trace_id"])
    spans = ctx.store.get_spans(item["trace_id"])
    return json_response(
        {
            "queue": q,
            "configs": configs,
            "item": item,
            "trace": asdict(trace) if trace else None,
            "spans": [s.to_dict() for s in spans],
        }
    )


@router.post(
    "/api/projects/{project_id}/queues/{queue_id}/items/{item_id}/complete",
    response_model=m.ScoresSaved,
)
def complete(
    queue_id: str, item_id: str, body: m.CompleteRequest, ctx: Ctx = Depends(project_ctx)
) -> Response:
    ctx.require("member")
    try:
        ids = Annotation(ctx.store).complete(
            queue_id, item_id, ctx.actor, body.scores or {}, body.comment
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return json_response({"score_ids": ids})


@router.post(
    "/api/projects/{project_id}/queues/{queue_id}/items/{item_id}/skip", response_model=m.Ok
)
def skip(queue_id: str, item_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
    ctx.require("member")
    Annotation(ctx.store).skip(queue_id, item_id, ctx.actor)
    return json_response({"ok": True})


@router.get(
    "/api/projects/{project_id}/queues/{queue_id}/labels.jsonl",
    response_class=Response,
    responses={
        200: {"description": "One labelled item per line", "content": {"application/x-ndjson": {}}}
    },
)
def labels(queue_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
    try:
        rows = Annotation(ctx.store).labels(queue_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        "".join(dumps(r) + "\n" for r in rows),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename=labels-{queue_id}.jsonl"},
    )
