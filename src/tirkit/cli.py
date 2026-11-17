"""tirkit CLI: run evals, compare runs, gate CI, manage datasets and traces."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(help="Tirkit: LLM evals and observability.", no_args_is_help=True)
dataset_app = typer.Typer(help="Build, import, export and list datasets.", no_args_is_help=True)
traces_app = typer.Typer(help="Inspect stored traces.", no_args_is_help=True)
app.add_typer(dataset_app, name="dataset")
app.add_typer(traces_app, name="traces")

DB_OPTION = typer.Option(None, "--db", help="SQLite file (default: $TIRKIT_DB_PATH or tirkit.db).")


def _store(db: str | None) -> Any:
    """Local SQLite store, or the server's store when $TIRKIT_HOST is set and --db is not."""
    if not db and os.environ.get("TIRKIT_HOST"):
        import tirkit

        return tirkit.init().store
    from tirkit.store import SQLiteStore

    store = SQLiteStore(db or os.environ.get("TIRKIT_DB_PATH") or "tirkit.db")
    store.init_schema()
    return store


def _load_target(spec: str) -> Any:
    if ":" not in spec:
        raise typer.BadParameter("target must look like module.path:function")
    from tirkit.scorer_specs import load_object

    return load_object(spec)


def _load_dataset(spec: str, store: Any) -> Any:
    from tirkit.datasets import Dataset

    path = Path(spec)
    if path.suffix == ".jsonl" and path.exists():
        return Dataset.from_jsonl(path)
    if path.suffix == ".csv" and path.exists():
        return Dataset.from_csv(path)
    name, _, version = spec.partition("@")
    return store.load_dataset(name, version or None)


def build_scorer(spec: str, store: Any = None) -> Any:
    """Scorer from a short spec; see :func:`tirkit.scorer_specs.build_scorer`."""
    from tirkit.scorer_specs import UnknownScorer
    from tirkit.scorer_specs import build_scorer as build

    try:
        return build(spec, store)
    except UnknownScorer as exc:
        raise typer.BadParameter(str(exc)) from exc


def _load_run(ref: str, store: Any) -> Any:
    from tirkit.runner import RunResult

    if ref.endswith(".json") and Path(ref).exists():
        return RunResult.load_json(ref)
    return store.load_experiment(ref)


@app.command()
def run(
    dataset: str = typer.Argument(..., help="file.jsonl / file.csv, or a stored name[@version]"),
    target: str = typer.Argument(..., help="module.path:function taking the example input dict"),
    scorer: list[str] = typer.Option(["exact_match"], "--scorer", "-s", help="Repeatable."),
    name: str | None = typer.Option(None, help="Name for this run."),
    concurrency: int = typer.Option(4, help="Parallel target calls."),
    retries: int = typer.Option(0, help="Retries per failed target call."),
    out: Path | None = typer.Option(None, help="Also write the run as JSON (for CI baselines)."),
    db: str | None = DB_OPTION,
) -> None:
    """Run TARGET over DATASET, score it, store the run and print means with 95% CIs."""
    import tirkit
    from tirkit.report import render_run
    from tirkit.runner import run as run_eval

    store = _store(db)
    ds = _load_dataset(dataset, store)
    store.save_dataset(ds)
    client = tirkit.get_client() if hasattr(store, "http") else tirkit.init(path=store.path)
    assert client is not None
    fn = _load_target(target)
    scorers = [build_scorer(s, store) for s in scorer]
    result = run_eval(
        ds, fn, scorers, name=name, store=client.store, concurrency=concurrency, retries=retries
    )
    if out:
        result.save_json(out)
    typer.echo(render_run(result))
    typer.echo(f"\nexperiment_id: {result.experiment_id}")
    client.shutdown()


@app.command()
def compare(
    baseline: str = typer.Argument(..., help="Experiment id or run JSON file."),
    candidate: str = typer.Argument(..., help="Experiment id or run JSON file."),
    scorer: list[str] | None = typer.Option(None, "--scorer", "-s"),
    fmt: str = typer.Option("md", "--format", help="md, json or html"),
    seed: int = typer.Option(0),
    db: str | None = DB_OPTION,
) -> None:
    """Paired comparison of two runs on the same dataset version, with 95% bootstrap CIs."""
    from tirkit.report import compare_json, markdown_to_html, render_compare
    from tirkit.runner import compare as compare_runs

    store = _store(db)
    a, b = _load_run(baseline, store), _load_run(candidate, store)
    comps = compare_runs(a, b, scorers=scorer or None, seed=seed)
    if fmt == "json":
        typer.echo(json.dumps(compare_json(comps), indent=1))
    elif fmt == "html":
        typer.echo(markdown_to_html(render_compare(comps, a, b)))
    else:
        typer.echo(render_compare(comps, a, b))


@app.command()
def gate(
    baseline: str = typer.Argument(..., help="Experiment id or run JSON file."),
    candidate: str = typer.Argument(..., help="Experiment id or run JSON file."),
    scorer: str = typer.Option(..., "--scorer", "-s", help="Scorer the gate decides on."),
    max_drop: float = typer.Option(0.0, "--max-drop", help="Allowed drop before failing."),
    report: Path | None = typer.Option(None, help="Write a Markdown report (PR comment)."),
    seed: int = typer.Option(0),
    db: str | None = DB_OPTION,
) -> None:
    """Exit 1 only if the candidate is confidently worse than baseline by more than --max-drop."""
    from tirkit.report import render_compare
    from tirkit.runner import compare as compare_runs
    from tirkit.runner import gate as gate_runs

    store = _store(db)
    a, b = _load_run(baseline, store), _load_run(candidate, store)
    passed = gate_runs(a, b, scorer, max_drop=max_drop, seed=seed)
    comps = compare_runs(a, b, seed=seed)
    md = render_compare(comps, a, b, gate_scorer=scorer, passed=passed, max_drop=max_drop)
    if report:
        report.write_text(md)
    typer.echo(md)
    raise typer.Exit(code=0 if passed else 1)


@dataset_app.command("build")
def dataset_build(
    name: str = typer.Option(..., help="Dataset name."),
    from_traces: bool = typer.Option(True, "--from-traces/--no-from-traces"),
    span_name: str | None = typer.Option(None, "--span-name", help="Only spans with this name."),
    root_only: bool = typer.Option(True, help="Only root spans (whole requests)."),
    since_hours: float | None = typer.Option(None, help="Only spans from the last N hours."),
    limit: int = typer.Option(10_000),
    expected_from_output: bool = typer.Option(False, help="Use current outputs as expected."),
    threshold: float = typer.Option(0.9, help="Near-duplicate Jaccard threshold."),
    db: str | None = DB_OPTION,
) -> None:
    """Mine a redacted, de-duplicated, versioned dataset from stored traces."""
    from tirkit.datasets import from_traces as mine

    store = _store(db)
    where: dict[str, Any] = {}
    if span_name:
        where["name"] = span_name
    if root_only:
        where["root"] = True
    if since_hours:
        where["start_after"] = time.time() - since_hours * 3600
    spans = store.query_spans(where, limit=limit)
    ds = mine(spans, name=name, threshold=threshold, expected_from_output=expected_from_output)
    version = store.save_dataset(ds, note=f"from {len(spans)} spans")
    typer.echo(f"{name}@{version}: {len(ds)} examples from {len(spans)} spans")


@dataset_app.command("import")
def dataset_import(
    path: Path, name: str | None = typer.Option(None), db: str | None = DB_OPTION
) -> None:
    """Import a JSONL or CSV file as a dataset version."""
    store = _store(db)
    ds = _load_dataset(str(path), store)
    if name:
        ds.name = name
    typer.echo(f"{ds.name}@{store.save_dataset(ds)}: {len(ds)} examples")


@dataset_app.command("export")
def dataset_export(
    ref: str = typer.Argument(..., help="name[@version]"),
    out: Path = typer.Option(..., help="Output .jsonl file."),
    db: str | None = DB_OPTION,
) -> None:
    store = _store(db)
    ds = _load_dataset(ref, store)
    ds.to_jsonl(out)
    typer.echo(f"wrote {len(ds)} examples to {out}")


@dataset_app.command("list")
def dataset_list(db: str | None = DB_OPTION) -> None:
    for d in _store(db).list_datasets():
        typer.echo(
            f"{d['name']}@{d['latest_version']}  {d['n_items']} items  {d['n_versions']} versions"
        )


@traces_app.command("list")
def traces_list(limit: int = typer.Option(20), db: str | None = DB_OPTION) -> None:
    for t in _store(db).query_traces(limit=limit):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.start or 0))
        cost = f"${t.cost_usd:.5f}" if t.cost_usd else "-"
        err = " ERROR" if t.has_error else ""
        dur = f"{t.duration_ms:.0f}ms" if t.duration_ms else "-"
        typer.echo(f"{t.trace_id}  {when}  {t.name}  {t.n_spans} spans  {dur}  {cost}{err}")


@traces_app.command("show")
def traces_show(trace_id: str, db: str | None = DB_OPTION) -> None:
    spans = _store(db).get_spans(trace_id)
    if not spans:
        raise typer.BadParameter(f"no trace {trace_id}")
    depth: dict[str | None, int] = {None: -1}
    for s in spans:
        depth[s.span_id] = depth.get(s.parent_id, -1) + 1
        pad = "  " * depth[s.span_id]
        extra = f" model={s.model}" if s.model else ""
        cost = f" ${s.cost_usd:.5f}" if s.cost_usd else ""
        typer.echo(f"{pad}- {s.name} [{s.kind}] {s.duration_ms or 0:.1f}ms{extra}{cost} {s.status}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to bind."),
    port: int = typer.Option(8000),
    database_url: str | None = typer.Option(
        None, help="postgresql://... or a SQLite path (default: $TIRKIT_DATABASE_URL)."
    ),
    media_dir: Path | None = typer.Option(None, help="Where media files are stored."),
    workers: int = typer.Option(1, help="Uvicorn worker processes."),
) -> None:
    """Run the Tirkit server (API, OTLP ingest and web UI).

    Needs ``pip install "tirkit[server]"``.
    """
    try:
        import uvicorn
    except ImportError as exc:
        raise typer.BadParameter('install the server extra: pip install "tirkit[server]"') from exc
    if database_url:
        os.environ["TIRKIT_DATABASE_URL"] = database_url
    if media_dir:
        os.environ["TIRKIT_MEDIA_DIR"] = str(media_dir)
    if workers > 1 and os.environ.get("TIRKIT_EMBEDDED_WORKER") is None:
        os.environ["TIRKIT_EMBEDDED_WORKER"] = "0"  # one worker per process would be wasteful
        typer.echo("Multiple server workers: run `tirkit worker` separately for background jobs.")
    if host in ("127.0.0.1", "localhost") and "TIRKIT_INSECURE_COOKIES" not in os.environ:
        os.environ["TIRKIT_INSECURE_COOKIES"] = "1"  # plain-HTTP local use
    typer.echo(f"Tirkit server on http://{host}:{port}  (API docs: /api/docs)")
    uvicorn.run("tirkit.server.main:app", host=host, port=port, workers=workers, proxy_headers=True)


@app.command()
def worker(
    database_url: str | None = typer.Option(None, help="Defaults to $TIRKIT_DATABASE_URL."),
    poll: float = typer.Option(1.0, help="Seconds between polls when idle."),
) -> None:
    """Run the background worker: UI experiments, online evals, alerts and retention."""
    import logging

    from tirkit.jobs import Worker
    from tirkit.store import open_store

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    store = open_store(database_url or os.environ.get("TIRKIT_DATABASE_URL", "tirkit-server.db"))
    store.init_schema()
    typer.echo("Tirkit worker running (Ctrl-C to stop)")
    Worker(store, poll_s=poll).run_forever()


@app.command("judge-report")
def judge_report(
    labels: Path = typer.Argument(
        ..., help="JSONL: {input, output, labels: {name: value}} per line."
    ),
    label: str = typer.Option(..., "--label", "-l", help="Human label to compare against."),
    rubric: str = typer.Option("correctness", help="Built-in rubric name, or path to a text file."),
    model: str | None = typer.Option(None, help="Judge model ($JUDGE_MODEL by default)."),
    pairwise: bool = typer.Option(True, help="Also judge pairs in both orders (flip rate)."),
    out: Path | None = typer.Option(None, help="Write the scored rows and report as JSON."),
    db: str | None = DB_OPTION,
) -> None:
    """Agreement (kappa), flip rate and length bias of the LLM judge against your labels.

    Rows that already carry a ``judge`` value are reported as they are; otherwise the judge runs
    on an OpenAI-compatible endpoint (LLM_BASE_URL/LLM_API_KEY) and the result is stored with
    its rubric version.
    """
    from tirkit.judge import RUBRICS, LLMJudge, OpenAICompatibleJudge, Rubric
    from tirkit.reliability import (
        judge_labels,
        load_labels,
        reliability_report,
        render_reliability,
        save_report,
    )

    rows = load_labels(labels)
    if all("judge" in r for r in rows):
        scored = rows
        rub = None
    else:
        store = _store(db)
        rub = (
            Rubric(Path(rubric).stem, Path(rubric).read_text())
            if Path(rubric).exists()
            else RUBRICS[rubric]
        )
        judge_model = OpenAICompatibleJudge(model)
        scored = judge_labels(
            rows, LLMJudge(judge_model, rub, cache=store), human_key=label, pairwise=pairwise
        )
    rep = reliability_report(scored, human_key=label)
    if rub is not None:
        save_report(_store(db), rep, rub, model or "default", source=str(labels))
        typer.echo(f"rubric {rub.name}@{rub.version}")
    typer.echo(render_reliability(rep))
    if out:
        out.write_text(json.dumps({"report": rep, "rows": scored}, indent=1, default=str))


if __name__ == "__main__":
    app()
