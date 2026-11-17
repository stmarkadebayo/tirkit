"""Prompts, playground and experiments started from the UI (SPEC FR-14, FR-20, FR-21)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response

from tirkit import jobs
from tirkit.prompts import MissingVariable, Prompt, PromptStore
from tirkit.server.app import Ctx, extension


@extension
def register(app: FastAPI, project_ctx: Callable[..., Ctx], key_project: Any, json: Any) -> None:
    store = app.state.store

    def author(ctx: Ctx) -> str | None:
        return ctx.user.email if ctx.user else "api"

    # -- prompts ---------------------------------------------------------------------------------

    @app.get("/api/projects/{project_id}/prompts")
    def list_prompts(ctx: Ctx = Depends(project_ctx)) -> Response:
        return json(PromptStore(ctx.store).list())

    @app.get("/api/projects/{project_id}/prompts/{name:path}/versions")
    def prompt_versions(name: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        versions = PromptStore(ctx.store).versions(name)
        if not versions:
            raise HTTPException(404, f"no prompt {name!r}")
        return json({"name": name, "versions": versions})

    @app.post("/api/projects/{project_id}/prompts/{name:path}/versions")
    async def create_version(
        name: str, request: Request, ctx: Ctx = Depends(project_ctx)
    ) -> Response:
        ctx.require("member")
        b = await request.json()
        try:
            v = PromptStore(ctx.store).create_version(
                name,
                b.get("template"),
                kind=b.get("kind", "chat"),
                config=b.get("config") or {},
                message=b.get("message"),
                labels=b.get("labels") or [],
                author=author(ctx),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return json({"name": name, "version": v}, 201)

    @app.put("/api/projects/{project_id}/prompts/{name:path}/labels/{label}")
    async def set_label(
        name: str, label: str, request: Request, ctx: Ctx = Depends(project_ctx)
    ) -> Response:
        ctx.require("member")
        b = await request.json()
        try:
            PromptStore(ctx.store).set_label(name, label, int(b["version"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return json({"name": name, "label": label, "version": int(b["version"])})

    @app.get("/api/v1/prompts/{name:path}")
    def sdk_prompt(name: str, request: Request, pid: str = Depends(key_project)) -> Response:
        q = request.query_params
        try:
            p = PromptStore(store.for_project(pid)).get(
                name,
                label=q.get("label") or None,
                version=int(q["version"]) if q.get("version") else None,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return json(
            {
                "name": p.name,
                "version": p.version,
                "kind": p.kind,
                "template": p.template,
                "config": p.config,
                "labels": p.labels,
            }
        )

    # -- playground ------------------------------------------------------------------------------

    @app.get("/api/projects/{project_id}/playground/models")
    def playground_models(ctx: Ctx = Depends(project_ctx)) -> Response:
        from tirkit.providers import list_models, providers_status

        return json({"providers": providers_status(), "models": list_models()})

    @app.post("/api/projects/{project_id}/playground/run")
    async def playground_run(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        """Compile a template with variables, call the model, trace it in this project."""
        from tirkit.providers import ProviderNotConfigured, call_model
        from tirkit.tracing import sink_context

        ctx.require("member")
        b = await request.json()
        kind = b.get("kind", "chat")
        prompt = Prompt(
            b.get("prompt_name") or "playground",
            int(b.get("prompt_version") or 0),
            kind,
            b.get("template"),
            {},
        )
        try:
            compiled = prompt.compile(**(b.get("variables") or {}))
        except MissingVariable as exc:
            raise HTTPException(400, f"missing variable: {exc}") from exc
        except (TypeError, AttributeError) as exc:
            raise HTTPException(400, f"invalid template: {exc}") from exc
        messages = compiled if kind == "chat" else [{"role": "user", "content": compiled}]
        model = b.get("model")
        if not model:
            raise HTTPException(400, "pick a model")
        try:
            with sink_context(ctx.store):
                if prompt.version:
                    with prompt.use():
                        res = call_model(
                            model, messages, b.get("params") or {}, span_name="playground"
                        )
                else:
                    res = call_model(model, messages, b.get("params") or {}, span_name="playground")
        except ProviderNotConfigured as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"model call failed: {exc}") from exc
        recent = ctx.store.query_spans({"name": "playground"}, limit=1)
        return json(
            {
                "output": res.text,
                "model": res.model,
                "usage": res.usage,
                "cost_usd": res.cost_usd,
                "latency_ms": res.latency_ms,
                "trace_id": recent[0].trace_id if recent else None,
            }
        )

    # -- experiments started from the UI -------------------------------------------------------

    @app.post("/api/projects/{project_id}/experiments/run")
    async def start_experiment(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        ctx.require("member")
        b = await request.json()
        if not b.get("dataset"):
            raise HTTPException(400, "dataset required")
        if not b.get("prompt") and not b.get("template"):
            raise HTTPException(400, "prompt or template required")
        try:
            ctx.store.load_dataset(b["dataset"], b.get("version"))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        job_id = jobs.enqueue(ctx.store, "experiment", b)
        return json({"job_id": job_id}, 202)

    @app.get("/api/projects/{project_id}/jobs/{job_id}")
    def get_job(job_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
        job = jobs.get_job(ctx.store, job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        return json(job)

    @app.get("/api/projects/{project_id}/jobs")
    def list_jobs(request: Request, ctx: Ctx = Depends(project_ctx)) -> Response:
        return json(jobs.list_jobs(ctx.store, request.query_params.get("kind")))
