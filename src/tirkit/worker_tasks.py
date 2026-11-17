"""Job handlers and periodic tasks run by the worker."""

from __future__ import annotations

import logging
import time
from typing import Any

from tirkit.jobs import handler, periodic
from tirkit.store import SQLStore
from tirkit.tracing import sink_context

log = logging.getLogger("tirkit.worker")


def build_scorers(specs: list[str], store: SQLStore, judge_model: str | None) -> list[Any]:
    from tirkit.cli import build_scorer
    from tirkit.judge import LLMJudge
    from tirkit.providers import ServerJudgeModel

    out = []
    for spec in specs:
        if spec.startswith("judge:"):
            if not judge_model:
                raise ValueError("judge scorers need a judge model")
            out.append(LLMJudge(ServerJudgeModel(judge_model), spec.split(":", 1)[1], cache=store))
        elif ":" in spec:
            raise ValueError(f"scorer {spec!r} is not allowed from the UI")
        else:
            out.append(build_scorer(spec, store))
    return out


@handler("experiment")
def run_experiment(store: SQLStore, p: dict[str, Any]) -> dict[str, Any]:
    """Run a prompt version (or inline template) with a model over a dataset version."""
    from tirkit.prompts import Prompt, PromptStore
    from tirkit.providers import call_model
    from tirkit.runner import run

    ds = store.load_dataset(p["dataset"], p.get("version"))
    if p.get("prompt"):
        prompt = PromptStore(store).get(p["prompt"]["name"], version=int(p["prompt"]["version"]))
    else:
        prompt = Prompt("inline", 0, p.get("kind", "chat"), p["template"], {})
    model = p.get("model") or prompt.config.get("model")
    if not model:
        raise ValueError("no model given")
    params = {**prompt.config.get("params", {}), **(p.get("params") or {})}
    scorers = build_scorers(p.get("scorers") or ["exact_match"], store, p.get("judge_model"))

    def target(inp: dict[str, Any]) -> str:
        compiled = prompt.compile(**inp)
        messages = compiled if prompt.kind == "chat" else [{"role": "user", "content": compiled}]
        with prompt.use():
            return call_model(model, messages, params, span_name=f"prompt:{prompt.name}").text

    with sink_context(store):
        result = run(
            ds,
            target,
            scorers,
            name=p.get("name") or f"{prompt.ref} · {model}",
            concurrency=int(p.get("concurrency", 4)),
            config={
                "prompt_version": prompt.ref if prompt.version else None,
                "model": model,
                "params": params,
                "started_from": "ui",
            },
        )
        result.target = f"{prompt.ref} · {model}"
        for i, tid in enumerate(result.trace_ids):
            t = store.get_trace(tid) if tid else None
            result.costs_usd.append(t.cost_usd if t else None)
            _ = i
        store.save_experiment(result)
    return {"experiment_id": result.experiment_id, "summary": result.summary(n_resamples=2000)}


@periodic(every_s=3600)
def retention(root: SQLStore) -> None:
    """Delete traces older than each project's retention setting."""
    rows = root.execute("SELECT project_id, settings FROM projects")
    for pid, settings in rows:
        days = (root.dialect.load_json(settings) or {}).get("retention_days")
        if days:
            n = root.for_project(pid).delete_before(time.time() - float(days) * 86400)
            if n:
                log.info("retention: deleted %d spans from %s", n, pid)


@periodic(every_s=20)
def online_evals(root: SQLStore) -> None:
    from tirkit.online import run_all

    run_all(root)


@periodic(every_s=60)
def alerts(root: SQLStore) -> None:
    from tirkit.alerts import check_all

    check_all(root)


@handler("judge_report")
def judge_report_job(store: SQLStore, p: dict[str, Any]) -> dict[str, Any]:
    """Run a judge over a queue's human labels and store the reliability report."""
    from tirkit.annotation import Annotation
    from tirkit.judge import LLMJudge, Rubric
    from tirkit.providers import ServerJudgeModel
    from tirkit.reliability import judge_labels, reliability_report, save_report

    rows = Annotation(store).labels(p["queue_id"])
    rubric = Rubric(p.get("rubric_name") or "custom", p["rubric_text"])
    judge = LLMJudge(ServerJudgeModel(p["model"]), rubric, cache=store)
    with sink_context(store):
        scored = judge_labels(
            rows, judge, human_key=p["label"], pairwise=bool(p.get("pairwise", True))
        )
    report = reliability_report(scored, human_key=p["label"])
    report_id = save_report(store, report, rubric, p["model"], source=f"queue:{p['queue_id']}")
    return {"report_id": report_id, "summary": report}
