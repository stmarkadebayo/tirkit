"""Judge reliability: is the LLM judge trustworthy? (SPEC FR-19, §6.4–6.5, experiment E1)

Inputs are human-labelled items: ``{"input", "output", "labels": {name: value}}`` (the export of
an annotation queue, or your own JSONL). The report compares a judge's scores with the human
label on the same items:

- **Cohen's kappa** on the labels as given, with a 95% bootstrap interval, and
  **quadratic-weighted kappa** for ordinal 1–5 scores;
  pass/fail kappa after mapping scores ≥ ``pass_at`` to pass;
- **exact agreement** and mean absolute difference;
- **length bias**: Spearman correlation of output length (words) with the judge score, next to the
  same correlation for human scores;
- **position-swap flip rate** (when pairs were judged in both orders): share of non-tie pairs whose
  preferred answer changes when the order changes.

Reports are stored per rubric version, so calibration progress is visible.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tirkit import stats
from tirkit.datasets import Example
from tirkit.scorers import output_text


def load_labels(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, str):
        low = v.strip().lower()
        if low in {"pass", "true", "yes", "good"}:
            return 1.0
        if low in {"fail", "false", "no", "bad"}:
            return 0.0
        try:
            return float(low)
        except ValueError:
            return None
    return None


def judge_labels(
    rows: Sequence[dict[str, Any]],
    judge: Any,
    *,
    human_key: str,
    pairwise: bool = True,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Score every labelled item with the judge. With ``pairwise``, also pair items that share an
    input (or random items) and judge each pair in both orders to measure position bias."""
    out = []
    for r in rows:
        if r.get("labels", {}).get(human_key) is None:
            continue
        inp = r.get("input")
        example = Example(inp if isinstance(inp, dict) else {"input": inp})
        sc = judge.score(example, r.get("output"))
        out.append({**r, "judge": sc.value, "judge_reason": sc.reason, "judge_error": sc.error})
    if pairwise and len(out) >= 2:
        rng = random.Random(seed)
        order = list(range(len(out)))
        rng.shuffle(order)
        pairs = []
        for a, b in zip(order[::2], order[1::2], strict=False):
            ex = Example({"task": "Answer the user's request well."})
            res = judge.pairwise_detail(ex, out[a].get("output"), out[b].get("output"))
            pairs.append(
                {
                    "a": a,
                    "b": b,
                    "first": res.first,
                    "second": res.second,
                    "flipped": res.flipped,
                    "verdict": res.verdict,
                    "error": res.error,
                }
            )
        out[0]["_pairs"] = pairs
    return out


def flip_rate(pairs: Sequence[dict[str, Any]]) -> tuple[float | None, int]:
    """Share of pairs whose preference changed with order; ties (in either order) excluded."""
    decided = [
        p
        for p in pairs
        if p.get("first") not in (None, "tie") and p.get("second") not in (None, "tie")
    ]
    if not decided:
        return None, 0
    return sum(bool(p["flipped"]) for p in decided) / len(decided), len(decided)


def reliability_report(
    scored: Sequence[dict[str, Any]], *, human_key: str, pass_at: float = 4.0
) -> dict[str, Any]:
    pairs = scored[0].get("_pairs", []) if scored else []
    usable = [
        (r, _num(r["labels"][human_key]), _num(r.get("judge")))
        for r in scored
        if _num(r["labels"].get(human_key)) is not None
    ]
    both: list[tuple[dict[str, Any], float, float]] = [
        (r, h, j) for r, h, j in usable if h is not None and j is not None
    ]
    report: dict[str, Any] = {
        "n_labelled": len(usable),
        "n_judged": len(both),
        "n_judge_failures": len(usable) - len(both),
        "human_key": human_key,
    }
    if both:
        h = [x[1] for x in both]
        j = [x[2] for x in both]
        report["exact_agreement"] = sum(a == b for a, b in zip(h, j, strict=True)) / len(both)
        report["mean_abs_diff"] = stats.mean([abs(a - b) for a, b in zip(h, j, strict=True)])
        rh, rj = [round(x) for x in h], [round(x) for x in j]
        report["kappa"] = stats.cohen_kappa(rh, rj)
        report["kappa_ci_low"], report["kappa_ci_high"] = stats.kappa_ci(rh, rj)
        ordinal = all(float(x).is_integer() for x in h + j)
        if ordinal:
            report["weighted_kappa"] = stats.weighted_kappa(
                [int(x) for x in h], [int(x) for x in j]
            )
        binary_h = [x >= (pass_at if max(h) > 1 else 1) for x in h]
        binary_j = [x >= (pass_at if max(j) > 1 else 1) for x in j]
        report["pass_fail_kappa"] = stats.cohen_kappa(binary_h, binary_j)
        lengths = [float(len(output_text(x[0].get("output")).split())) for x in both]
        if len(both) >= 3:
            report["length_corr_judge"] = stats.spearman(lengths, j)
            report["length_corr_human"] = stats.spearman(lengths, h)
    rate, n_decided = flip_rate(pairs)
    report["n_pairs"] = len(pairs)
    report["n_pairs_decided"] = n_decided
    report["flip_rate"] = rate
    return report


def save_report(store: Any, report: dict[str, Any], rubric: Any, model: str, source: str) -> str:
    rid = "jr_" + uuid.uuid4().hex[:10]
    store.save_rubric(rubric.name, rubric.version, rubric.text)
    store.execute(
        "INSERT INTO judge_reports (project_id, report_id, rubric_name, rubric_version, model, "
        "source, summary, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            store.project_id,
            rid,
            rubric.name,
            rubric.version,
            model,
            source,
            store._json(report),
            time.time(),
        ],
    )
    return rid


def list_reports(store: Any) -> list[dict[str, Any]]:
    rows = store.execute(
        "SELECT report_id, rubric_name, rubric_version, model, source, summary, created FROM "
        "judge_reports WHERE project_id = ? ORDER BY created DESC",
        [store.project_id],
    )
    keys = "report_id rubric_name rubric_version model source summary created".split()
    out = []
    for r in rows:
        d = dict(zip(keys, r, strict=True))
        d["summary"] = store.dialect.load_json(d["summary"])
        out.append(d)
    return out


def render_reliability(report: dict[str, Any]) -> str:
    def f(key: str, digits: int = 3) -> str:
        v = report.get(key)
        return "—" if v is None else f"{v:.{digits}f}"

    return "\n".join(
        [
            f"Judge reliability vs human label `{report['human_key']}`",
            f"  items labelled: {report['n_labelled']}, judged: {report['n_judged']}, "
            f"judge failures: {report['n_judge_failures']}",
            f"  Cohen's kappa: {f('kappa')}   weighted kappa: {f('weighted_kappa')}   "
            f"pass/fail kappa: {f('pass_fail_kappa')}",
            f"  exact agreement: {f('exact_agreement')}   "
            f"mean |judge − human|: {f('mean_abs_diff')}",
            f"  length bias (Spearman with output words): judge {f('length_corr_judge')}, "
            f"human {f('length_corr_human')}",
            f"  position-swap flip rate: {f('flip_rate')} over {report['n_pairs_decided']} decided "
            f"pairs ({report['n_pairs']} judged in both orders)",
        ]
    )
