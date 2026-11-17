"""Versioned datasets: list, read, mine from traces, replace, upload JSONL."""

from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from tirkit.server import models as m
from tirkit.server.deps import Ctx, app_settings, json_response, project_ctx, read_body
from tirkit.server.openapi import binary_body
from tirkit.server.settings import Settings
from tirkit.types import Dataset, Example, _example_from_row

router = APIRouter(tags=["datasets"])


@router.get("/api/projects/{project_id}/datasets", response_model=list[m.DatasetSummary])
def list_datasets(ctx: Ctx = Depends(project_ctx)) -> Response:
    return json_response(ctx.store.list_datasets())


@router.get("/api/projects/{project_id}/datasets/{name}", response_model=m.DatasetDetail)
def get_dataset(name: str, version: str | None = None, ctx: Ctx = Depends(project_ctx)) -> Response:
    """One version (latest by default) with its items and the list of versions."""
    try:
        ds = ctx.store.load_dataset(name, version)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return json_response(
        {
            "name": ds.name,
            "version": ds.version,
            "items": [asdict(e) for e in ds.examples],
            "versions": ctx.store.dataset_versions(name),
        }
    )


@router.post(
    "/api/projects/{project_id}/datasets/from-traces",
    response_model=m.DatasetSaved,
    status_code=201,
)
def dataset_from_traces(body: m.FromTracesRequest, ctx: Ctx = Depends(project_ctx)) -> Response:
    """Mine root spans into a new dataset version (redacted, near-duplicates removed)."""
    from tirkit.datasets import from_traces, remove_duplicates

    ctx.require("member")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    where = {k: v for k, v in (body.filter or {}).items() if v not in (None, "")}
    if body.trace_ids:
        spans = [
            s
            for tid in body.trace_ids[:5000]
            for s in ctx.store.get_spans(tid)
            if s.parent_id is None
        ]
    else:
        where.setdefault("root", True)
        spans = ctx.store.query_spans(where, limit=body.limit)
    ds = from_traces(
        spans,
        name=name,
        expected_from_output=body.expected_from_output,
        threshold=body.threshold,
    )
    if body.append:
        try:
            old = ctx.store.load_dataset(name)
            ds = Dataset(name, remove_duplicates([*old.examples, *ds.examples]))
        except KeyError:
            pass
    version = ctx.store.save_dataset(ds, description=body.description)
    return json_response({"name": name, "version": version, "n_items": len(ds)}, 201)


@router.put("/api/projects/{project_id}/datasets/{name}", response_model=m.DatasetSaved)
def put_dataset(name: str, body: m.PutDatasetRequest, ctx: Ctx = Depends(project_ctx)) -> Response:
    """Replace a dataset's items (creates a new version; old versions stay readable)."""
    ctx.require("member")
    examples = [
        Example(
            i.input if isinstance(i.input, dict) else {"input": i.input},
            i.expected,
            i.metadata or {},
        )
        for i in body.items
    ]
    version = ctx.store.save_dataset(
        Dataset(name, examples), description=body.description, note=body.note
    )
    return json_response({"name": name, "version": version, "n_items": len(examples)})


@router.post(
    "/api/projects/{project_id}/datasets/{name}/upload",
    response_model=m.DatasetSaved,
    status_code=201,
    openapi_extra=binary_body(
        "application/x-ndjson", description="JSONL: one example per line", text=True
    ),
)
async def upload_dataset(
    name: str,
    request: Request,
    ctx: Ctx = Depends(project_ctx),
    settings: Settings = Depends(app_settings),
) -> Response:
    """Body: JSONL (one example per line)."""
    ctx.require("member")
    text = (await read_body(request, settings.max_ingest_bytes)).decode("utf-8")
    try:
        examples = [
            _example_from_row(json.loads(line)) for line in text.splitlines() if line.strip()
        ]
    except ValueError as exc:
        raise HTTPException(400, f"invalid JSONL: {exc}") from exc
    version = ctx.store.save_dataset(Dataset(name, examples))
    return json_response({"name": name, "version": version, "n_items": len(examples)}, 201)
