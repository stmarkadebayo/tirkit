"""Ingestion with API keys: spans (Tirkit JSON and OTLP), media and scores.

The span routes parse their bodies by hand instead of through pydantic: validating every span
with pydantic on the ingestion path would add latency for no gain, since ``Span.from_dict`` already
ignores unknown fields. The request schema is still published (``IngestRequest``).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from tirkit import pricing
from tirkit.media import LocalMediaStore
from tirkit.redact import redact_obj
from tirkit.server import models as m
from tirkit.server.deps import (
    Ctx,
    app_cache,
    app_media,
    app_settings,
    app_store,
    json_response,
    key_project,
    project_ctx,
    project_settings,
    read_body,
)
from tirkit.server.openapi import binary_body, json_body
from tirkit.server.prometheus import count_spans
from tirkit.server.settings import Settings
from tirkit.store import SQLStore
from tirkit.types import Span

router = APIRouter(tags=["ingest"])

_SHA = re.compile(r"^[0-9a-f]{64}$")


def _prepare(request: Request, spans: list[Span], pid: str) -> list[Span]:
    """Validate ids, fill in cost from prices and apply the project's redaction setting."""
    st = project_settings(request, pid)
    store = app_store(request)
    overrides = app_cache(request).get("prices", pid, store.for_project(pid).get_prices)
    extra = [(p["pattern"], p.get("label", "[REDACTED]")) for p in st.get("redact_patterns") or []]
    for s in spans:
        if not s.trace_id or not s.span_id:
            raise HTTPException(400, "every span needs trace_id and span_id")
        if s.cost_usd is None and s.usage:
            s.cost_usd = pricing.cost(s.model, s.usage, overrides)
        if st.get("redact"):
            s.input = redact_obj(s.input, extra)
            s.output = redact_obj(s.output, extra)
            s.attributes = redact_obj(s.attributes, extra)
    return spans


@router.post(
    "/api/v1/ingest", response_model=m.IngestResult, openapi_extra=json_body(m.IngestRequest)
)
async def ingest(
    request: Request,
    pid: str = Depends(key_project),
    settings: Settings = Depends(app_settings),
    store: SQLStore = Depends(app_store),
) -> Response:
    body = await read_body(request, settings.max_ingest_bytes)
    try:
        data = json.loads(body)
        spans = [Span.from_dict(d) for d in data.get("spans", [])]
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(400, f"invalid payload: {exc}") from exc
    store.for_project(pid).save_spans(_prepare(request, spans, pid))
    count_spans(request, "spans", len(spans))
    return json_response({"accepted": len(spans)})


@router.post(
    "/v1/traces",
    response_model=m.OtlpAccepted,
    summary="OTLP/HTTP trace export",
    openapi_extra=binary_body(
        "application/x-protobuf", "application/json", description="ExportTraceServiceRequest"
    ),
    responses={200: {"content": {"application/x-protobuf": {}}}},
)
async def otlp_traces(
    request: Request,
    pid: str = Depends(key_project),
    settings: Settings = Depends(app_settings),
    store: SQLStore = Depends(app_store),
) -> Response:
    """OpenTelemetry exporters post here (``OTEL_EXPORTER_OTLP_ENDPOINT=<server>``)."""
    from tirkit.otel import decode_request

    body = await read_body(request, settings.max_ingest_bytes)
    ctype = request.headers.get("content-type", "application/x-protobuf")
    try:
        spans = decode_request(body, ctype)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid OTLP payload: {exc}") from exc
    store.for_project(pid).save_spans(_prepare(request, spans, pid))
    count_spans(request, "otlp", len(spans))
    if "json" in ctype:
        return Response("{}", media_type="application/json")
    return Response(b"", media_type="application/x-protobuf")


@router.put(
    "/api/v1/media/{sha256}",
    response_model=m.MediaStored,
    status_code=201,
    openapi_extra=binary_body("*/*", description="The raw bytes; sha256 must match."),
)
async def put_media(
    sha256: str,
    request: Request,
    pid: str = Depends(key_project),
    settings: Settings = Depends(app_settings),
    store: SQLStore = Depends(app_store),
    media: LocalMediaStore = Depends(app_media),
) -> Response:
    if not _SHA.match(sha256):
        raise HTTPException(400, "bad hash")
    data = await read_body(request, settings.max_media_bytes)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise HTTPException(400, "content does not match hash")
    mime = request.headers.get("content-type") or "application/octet-stream"
    media.put(data, mime)
    store.for_project(pid).record_media(sha256, mime, len(data))
    return json_response({"sha256": sha256, "size": len(data)}, 201)


MEDIA_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"description": "The stored bytes", "content": {"application/octet-stream": {}}}
}


def _serve_media(st: SQLStore, media: LocalMediaStore, sha256: str) -> Response:
    if not _SHA.match(sha256):
        raise HTTPException(400, "bad hash")
    info = st.media_info(sha256)
    data = media.get(sha256) if info else None
    if data is None or info is None:
        raise HTTPException(404, "media not found")
    return Response(
        data,
        media_type=info["mime"] or "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, max-age=86400, immutable",
        },
    )


@router.get("/api/v1/media/{sha256}", response_class=Response, responses=MEDIA_RESPONSE)
def get_media_key(
    sha256: str,
    pid: str = Depends(key_project),
    store: SQLStore = Depends(app_store),
    media: LocalMediaStore = Depends(app_media),
) -> Response:
    return _serve_media(store.for_project(pid), media, sha256)


@router.get(
    "/api/projects/{project_id}/media/{sha256}", response_class=Response, responses=MEDIA_RESPONSE
)
def get_media(
    sha256: str, ctx: Ctx = Depends(project_ctx), media: LocalMediaStore = Depends(app_media)
) -> Response:
    return _serve_media(ctx.store, media, sha256)


def _save_score(st: SQLStore, body: m.ScoreRequest, author: str | None, source: str) -> Response:
    if not body.name:
        raise HTTPException(400, "name required")
    if body.value is None and body.value_str is None:
        raise HTTPException(400, "value or value_str required")
    if not body.trace_id:
        raise HTTPException(400, "trace_id required")
    sid = st.save_score(
        body.name,
        body.value,
        trace_id=body.trace_id,
        span_id=body.span_id,
        value_str=body.value_str,
        source=body.source or source,
        reason=body.comment,
        author=author,
    )
    return json_response({"score_id": sid}, 201)


@router.post("/api/v1/scores", response_model=m.ScoreCreated, status_code=201)
def post_score_key(
    body: m.ScoreRequest, pid: str = Depends(key_project), store: SQLStore = Depends(app_store)
) -> Response:
    """Feedback or an external score on a trace (default source ``api``)."""
    return _save_score(store.for_project(pid), body, None, "api")


@router.post("/api/projects/{project_id}/scores", response_model=m.ScoreCreated, status_code=201)
def post_score(body: m.ScoreRequest, ctx: Ctx = Depends(project_ctx)) -> Response:
    """A score from a signed-in user (default source ``human``)."""
    ctx.require("member")
    author = ctx.user.email if ctx.user else None
    return _save_score(ctx.store, body, author, "human")
