"""Prompts (versions and labels), SDK prompt fetch, and the playground."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from tirkit.prompts import MissingVariable, Prompt, PromptStore
from tirkit.server import models as m
from tirkit.server.deps import Ctx, app_store, json_response, key_project, project_ctx
from tirkit.store import SQLStore

router = APIRouter(tags=["prompts"])


@router.get("/api/projects/{project_id}/prompts", response_model=list[m.PromptSummary])
def list_prompts(ctx: Ctx = Depends(project_ctx)) -> Response:
    return json_response(PromptStore(ctx.store).list())


@router.get(
    "/api/projects/{project_id}/prompts/{name:path}/versions", response_model=m.PromptVersions
)
def prompt_versions(name: str, ctx: Ctx = Depends(project_ctx)) -> Response:
    versions = PromptStore(ctx.store).versions(name)
    if not versions:
        raise HTTPException(404, f"no prompt {name!r}")
    return json_response({"name": name, "versions": versions})


@router.post(
    "/api/projects/{project_id}/prompts/{name:path}/versions",
    response_model=m.PromptVersionCreated,
    status_code=201,
)
def create_version(
    name: str, body: m.CreatePromptVersionRequest, ctx: Ctx = Depends(project_ctx)
) -> Response:
    ctx.require("member")
    try:
        v = PromptStore(ctx.store).create_version(
            name,
            body.template,
            kind=body.kind,
            config=body.config or {},
            message=body.message,
            labels=body.labels or [],
            author=ctx.actor,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return json_response({"name": name, "version": v}, 201)


@router.put(
    "/api/projects/{project_id}/prompts/{name:path}/labels/{label}", response_model=m.LabelSet
)
def set_label(
    name: str, label: str, body: m.SetLabelRequest, ctx: Ctx = Depends(project_ctx)
) -> Response:
    """Point a label (``production``, ``staging``, ...) at a version."""
    ctx.require("member")
    try:
        PromptStore(ctx.store).set_label(name, label, body.version)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return json_response({"name": name, "label": label, "version": body.version})


@router.get("/api/v1/prompts/{name:path}", response_model=m.SdkPrompt)
def sdk_prompt(
    name: str,
    label: str | None = Query(None, description="Default: production, else latest"),
    version: int | None = None,
    pid: str = Depends(key_project),
    store: SQLStore = Depends(app_store),
) -> Response:
    """Fetch a prompt with an API key (``tirkit.get_prompt``)."""
    try:
        p = PromptStore(store.for_project(pid)).get(name, label=label or None, version=version)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return json_response(
        {
            "name": p.name,
            "version": p.version,
            "kind": p.kind,
            "template": p.template,
            "config": p.config,
            "labels": p.labels,
        }
    )


@router.get("/api/projects/{project_id}/playground/models", response_model=m.PlaygroundModels)
def playground_models(ctx: Ctx = Depends(project_ctx)) -> Response:
    from tirkit.providers import list_models, providers_status

    return json_response({"providers": providers_status(), "models": list_models()})


@router.post("/api/projects/{project_id}/playground/run", response_model=m.PlaygroundRun)
def playground_run(body: m.PlaygroundRunRequest, ctx: Ctx = Depends(project_ctx)) -> Response:
    """Compile a template with variables, call the model, trace it in this project."""
    from tirkit.providers import ProviderNotConfigured, call_model
    from tirkit.tracing import sink_context

    ctx.require("member")
    prompt = Prompt(
        body.prompt_name or "playground", body.prompt_version or 0, body.kind, body.template, {}
    )
    try:
        compiled = prompt.compile(**(body.variables or {}))
    except MissingVariable as exc:
        raise HTTPException(400, f"missing variable: {exc}") from exc
    except (TypeError, AttributeError) as exc:
        raise HTTPException(400, f"invalid template: {exc}") from exc
    messages = compiled if body.kind == "chat" else [{"role": "user", "content": compiled}]
    if not body.model:
        raise HTTPException(400, "pick a model")
    params = body.params or {}
    try:
        with sink_context(ctx.store):
            if prompt.version:
                with prompt.use():
                    res = call_model(body.model, messages, params, span_name="playground")
            else:
                res = call_model(body.model, messages, params, span_name="playground")
    except ProviderNotConfigured as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"model call failed: {exc}") from exc
    recent = ctx.store.query_spans({"name": "playground"}, limit=1)
    return json_response(
        {
            "output": res.text,
            "model": res.model,
            "usage": res.usage,
            "cost_usd": res.cost_usd,
            "latency_ms": res.latency_ms,
            "trace_id": recent[0].trace_id if recent else None,
        }
    )
