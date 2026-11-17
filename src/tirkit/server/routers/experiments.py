"""Experiments (stored eval runs), comparisons, UI-started runs and the job queue."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from tirkit import jobs
from tirkit.server import models as m
from tirkit.server.deps import Ctx, json_response, project_ctx
from tirkit.types import RunResult

router = APIRouter(tags=["experiments"])


@router.post(
    "/api/projects/{project_id}/experiments", response_model=m.ExperimentCreated, status_code=201
)
def post_experiment(body: m.RunResultIn, ctx: Ctx = Depends(project_ctx)) -> Response:
    """Store a finished run (``RunResult.to_json()``) from the SDK or CLI."""
    ctx.require("member")
    try:
        result = RunResult.from_json(body.model_dump(exclude_unset=True))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"invalid run: {exc}") from exc
    ctx.store.save_experiment(result)
    return json_response({"experiment_id": result.experiment_id}, 201)


@router.get("/api/projects/{project_id}/experiments", response_model=list[m.ExperimentSummary])
def list_experiments(dataset: str | None = None, ctx: Ctx = Depends(project_ctx)) -> Response:
    rows = ctx.store.list_experiments(dataset)
    out = []
    for r in rows:
        try:
            res = ctx.store.load_experiment(r["experiment_id"])
            r["summary"] = res.summary(n_resamples=2000)
            r["n_items"] = len(res.outputs)
        except KeyError:
            pass
        out.append(r)
    return json_response(out)


@router.get(
    "/api/projects/{project_id}/experiments/{experiment_id}", response_model=m.ExperimentDetail
)
def get_experiment(experiment_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
    try:
        res = ctx.store.load_experiment(experiment_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        ds = ctx.store.load_dataset(res.dataset, res.dataset_version)
        inputs = [asdict(e) for e in ds.examples]
    except (KeyError, ValueError):
        inputs = []
    return json_response({**res.to_json(), "summary": res.summary(), "examples": inputs})


@router.get("/api/projects/{project_id}/compare", response_model=m.CompareResult)
def compare_experiments(
    baseline: str = Query(description="Experiment id"),
    candidate: str = Query(description="Experiment id"),
    ctx: Ctx = Depends(project_ctx),
) -> Response:
    """Paired comparison of two runs on the same dataset version, with per-item detail."""
    from tirkit.report import compare_json, render_compare
    from tirkit.runner import compare

    try:
        a = ctx.store.load_experiment(baseline)
        b = ctx.store.load_experiment(candidate)
        comps = compare(a, b)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        examples = [asdict(e) for e in ctx.store.load_dataset(a.dataset, a.dataset_version)]
    except (KeyError, ValueError):
        examples = []
    items = [
        {
            "idx": i,
            "example": examples[i] if i < len(examples) else None,
            "baseline": {
                "output": a.outputs[i],
                "trace_id": a.trace_ids[i] if i < len(a.trace_ids) else None,
                "scores": {k: v[i] for k, v in a.scores.items()},
            },
            "candidate": {
                "output": b.outputs[i],
                "trace_id": b.trace_ids[i] if i < len(b.trace_ids) else None,
                "scores": {k: v[i] for k, v in b.scores.items()},
            },
        }
        for i in range(len(a.outputs))
    ]
    return json_response(
        {
            "baseline": a.experiment_id,
            "candidate": b.experiment_id,
            "comparisons": compare_json(comps),
            "markdown": render_compare(comps, a, b),
            "items": items,
        }
    )


@router.post(
    "/api/projects/{project_id}/experiments/run", response_model=m.JobQueued, status_code=202
)
def start_experiment(body: m.ExperimentRunRequest, ctx: Ctx = Depends(project_ctx)) -> Response:
    """Queue a run of a prompt version (or inline template) over a dataset."""
    ctx.require("member")
    b = body.model_dump(exclude_unset=True)
    if not b.get("dataset"):
        raise HTTPException(400, "dataset required")
    if not b.get("prompt") and not b.get("template"):
        raise HTTPException(400, "prompt or template required")
    try:
        ctx.store.load_dataset(b["dataset"], b.get("version"))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return json_response({"job_id": jobs.enqueue(ctx.store, "experiment", b)}, 202)


@router.get("/api/projects/{project_id}/jobs/{job_id}", response_model=m.Job)
def get_job(job_id: str, ctx: Ctx = Depends(project_ctx)) -> Response:
    job = jobs.get_job(ctx.store, job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return json_response(job)


@router.get("/api/projects/{project_id}/jobs", response_model=list[m.JobSummary])
def list_jobs(kind: str | None = None, ctx: Ctx = Depends(project_ctx)) -> Response:
    return json_response(jobs.list_jobs(ctx.store, kind))
