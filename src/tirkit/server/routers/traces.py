"""Reading traces and spans: lists, detail, sessions, users, filters, metrics and export."""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from tirkit.serialize import dumps
from tirkit.server import models as m
from tirkit.server.deps import Ctx, json_response, project_ctx

router = APIRouter(tags=["traces"])


def trace_filters(
    name: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    release: str | None = None,
    tag: str | None = Query(None, description="Span-level: spans with this tag"),
    search: str | None = Query(None, description="Full-text search over name, input, output"),
    model: str | None = Query(None, description="Span-level"),
    kind: str | None = Query(None, description="Span-level"),
    start_after: float | None = Query(None, description="Unix seconds"),
    start_before: float | None = Query(None, description="Unix seconds"),
    min_cost: float | None = None,
    min_duration_ms: float | None = None,
    error: str | None = Query(None, description="true or false"),
) -> dict[str, Any]:
    """Trace and span filters from the query string (empty values are ignored)."""
    where: dict[str, Any] = {}
    texts = {
        "name": name,
        "user_id": user_id,
        "session_id": session_id,
        "release": release,
        "tag": tag,
        "search": search,
        "model": model,
        "kind": kind,
    }
    where.update({k: v for k, v in texts.items() if v})
    numbers = {
        "start_after": start_after,
        "start_before": start_before,
        "min_cost": min_cost,
        "min_duration_ms": min_duration_ms,
    }
    where.update({k: v for k, v in numbers.items() if v is not None})
    if error in ("true", "false"):
        where["error"] = error == "true"
    return where


@router.get("/api/projects/{project_id}/traces", response_model=m.TraceList)
def list_traces(
    where: dict[str, Any] = Depends(trace_filters),
    limit: int = Query(50, description="At most 500"),
    offset: int = 0,
    count: str | None = Query(None, description="false skips the total count"),
    ctx: Ctx = Depends(project_ctx),
) -> Response:
    for k in ("model", "kind", "tag"):
        where.pop(k, None)  # span-level filters are not on the traces table
    items = ctx.store.query_traces(where, limit=min(limit, 500), offset=offset)
    total = ctx.store.count_traces(where) if count != "false" else None
    return json_response({"items": [asdict(t) for t in items], "total": total})


TREE_PAGE = 10_000  # spans per page of the light view by default: a 10,000-span trace in one go
TREE_PAGE_MAX = 50_000
PREVIEW_CHARS = 2000
PREVIEW_MAX = 20_000
# Characters of input and output previews per page in all: a trace of up to 250 spans gets the
# full 2,000 characters per field, a 10,000-span trace 50, so the light view stays a few MB.
PREVIEW_BUDGET = 1_000_000


@router.get(
    "/api/projects/{project_id}/traces/{trace_id}",
    response_model=m.TraceDetail | m.TraceDetailFull,
)
def get_trace(
    trace_id: str,
    full: str | None = Query(None, description="1 or true: every span in full (old shape)"),
    limit: int = Query(TREE_PAGE, description=f"Spans per page, 1 to {TREE_PAGE_MAX}"),
    offset: int = Query(0, description="First span of the page (oldest first)"),
    preview: int = Query(PREVIEW_CHARS, description=f"Preview characters, 0 to {PREVIEW_MAX}"),
    ctx: Ctx = Depends(project_ctx),
) -> Response:
    """The trace, its scores and a light span tree (``TraceDetail``). Inputs and outputs are cut
    to ``preview`` characters, less on big pages (all previews of a page share a budget; the
    response says how many characters each got); get one full span from ``.../spans/{span_id}``.
    With ``full=1`` every span comes in full instead (``TraceDetailFull``)."""
    is_full = full in ("1", "true")
    trace = ctx.store.get_trace(trace_id) if is_full else ctx.store.get_trace_head(trace_id)
    if trace is None:
        raise HTTPException(404, "trace not found")
    scores = ctx.store.query_scores(trace_id=trace_id)
    if is_full:
        spans = [s.to_dict() for s in ctx.store.get_spans(trace_id)]
        return json_response({"trace": asdict(trace), "spans": spans, "scores": scores})
    head = {k: v for k, v in asdict(trace).items() if k not in ("input", "output")}
    limit = max(1, min(limit, TREE_PAGE_MAX))
    offset = max(0, offset)
    expected = max(1, min(limit, trace.n_spans - offset))
    preview = max(0, min(preview, PREVIEW_MAX, PREVIEW_BUDGET // (2 * expected)))
    rows, total = ctx.store.span_tree(trace_id, limit=limit, offset=offset, preview=preview)
    end = offset + len(rows)
    return json_response(
        {
            "trace": head,
            "spans": rows,
            "scores": scores,
            "total_spans": total,
            "offset": offset,
            "omitted": max(0, total - end),
            "preview": preview,
            "next_offset": end if end < total else None,
        }
    )


@router.get(
    "/api/projects/{project_id}/traces/{trace_id}/spans/{span_id}", response_model=m.Span
)
def get_span(trace_id: str, span_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
    """One span in full: input, output, parameters and attributes."""
    found = ctx.store.get_span(trace_id, span_id)
    if found is None:
        raise HTTPException(404, "span not found")
    return json_response(found.to_dict())


@router.get("/api/projects/{project_id}/spans", response_model=m.SpanList)
def list_spans(
    where: dict[str, Any] = Depends(trace_filters),
    limit: int = Query(50, description="At most 500"),
    offset: int = 0,
    ctx: Ctx = Depends(project_ctx),
) -> Response:
    spans = ctx.store.query_spans(where, limit=min(limit, 500), offset=offset)
    return json_response({"items": [s.to_dict() for s in spans]})


@router.get("/api/projects/{project_id}/sessions", response_model=list[m.SessionRow])
def list_sessions(
    limit: int = Query(100, description="At most 500"), ctx: Ctx = Depends(project_ctx)
) -> Response:
    return json_response(ctx.store.sessions(limit=min(limit, 500)))


@router.get("/api/projects/{project_id}/sessions/{session_id}", response_model=m.SessionDetail)
def get_session(session_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
    traces = ctx.store.query_traces({"session_id": session_id}, limit=500)
    traces.sort(key=lambda t: t.start or 0)
    return json_response({"session_id": session_id, "traces": [asdict(t) for t in traces]})


@router.get("/api/projects/{project_id}/users", response_model=list[m.UserRow])
def list_users(
    limit: int = Query(100, description="At most 500"), ctx: Ctx = Depends(project_ctx)
) -> Response:
    return json_response(ctx.store.users(limit=min(limit, 500)))


@router.get("/api/projects/{project_id}/filters", response_model=m.Filters)
def filters(ctx: Ctx = Depends(project_ctx)) -> Response:
    """Distinct values for the filter dropdowns."""
    return json_response(ctx.store.filter_values())


@router.get("/api/projects/{project_id}/metrics", response_model=m.Metrics)
def metrics(
    since: float | None = Query(None, description="Unix seconds (default: 24 h before until)"),
    until: float | None = Query(None, description="Unix seconds (default: now)"),
    buckets: int = Query(48, description="1 to 500"),
    group_by: str | None = Query(None, description="model, name, release, ..."),
    ctx: Ctx = Depends(project_ctx),
) -> Response:
    until = time.time() if until is None else until
    since = until - 24 * 3600 if since is None else since
    buckets = max(1, min(buckets, 500))
    return json_response(ctx.store.metrics(since, until, buckets=buckets, group_by=group_by))


@router.get(
    "/api/projects/{project_id}/export/traces.jsonl",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "One span per line",
            "content": {"application/x-ndjson": {"schema": {"$ref": "#/components/schemas/Span"}}},
        }
    },
)
def export_traces(
    where: dict[str, Any] = Depends(trace_filters), ctx: Ctx = Depends(project_ctx)
) -> StreamingResponse:
    def rows() -> Any:
        offset = 0
        while True:
            batch = ctx.store.query_spans(where, limit=1000, offset=offset, order="asc")
            if not batch:
                return
            for s in batch:
                yield dumps(s.to_dict()) + "\n"
            offset += len(batch)

    return StreamingResponse(
        rows(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=traces.jsonl"},
    )
