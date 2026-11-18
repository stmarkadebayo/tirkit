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
        n = len(both)
        h = [x[1] for x in both]
        j = [x[2] for x in both]
        matches = sum(a == b for a, b in zip(h, j, strict=True))
        report["exact_agreement"] = matches / n
        _put_ci(report, "exact_agreement", stats.wilson_ci(matches, n))
        report["mean_abs_diff"] = stats.mean([abs(a - b) for a, b in zip(h, j, strict=True)])
        rh, rj = [round(x) for x in h], [round(x) for x in j]
        report["kappa"] = stats.cohen_kappa(rh, rj)
        _put_ci(report, "kappa", stats.kappa_ci(rh, rj))
        ordinal = all(float(x).is_integer() for x in h + j)
        if ordinal:
            ih, ij = [int(x) for x in h], [int(x) for x in j]
            cats = sorted(set(ih) | set(ij))  # keep the full scale in every resample
            report["weighted_kappa"] = stats.weighted_kappa(ih, ij, categories=cats)
            _put_ci(
                report,
                "weighted_kappa",
                stats.bootstrap_stat(
                    n,
                    lambda idx: stats.weighted_kappa(
                        [ih[i] for i in idx], [ij[i] for i in idx], categories=cats
                    ),
                ),
            )
        binary_h = [x >= (pass_at if max(h) > 1 else 1) for x in h]
        binary_j = [x >= (pass_at if max(j) > 1 else 1) for x in j]
        report["pass_fail_kappa"] = stats.cohen_kappa(binary_h, binary_j)
        _put_ci(report, "pass_fail_kappa", stats.kappa_ci(binary_h, binary_j))
        lengths = [float(len(output_text(x[0].get("output")).split())) for x in both]
        if n >= 3:
            report["length_corr_judge"] = stats.spearman(lengths, j)
            report["length_corr_human"] = stats.spearman(lengths, h)
            report["length_bias"] = report["length_corr_judge"] - report["length_corr_human"]

            def length_stats(idx: list[int]) -> tuple[float, float, float]:
                ln = [lengths[i] for i in idx]
                cj = stats.spearman(ln, [j[i] for i in idx])
                ch = stats.spearman(ln, [h[i] for i in idx])
                return cj, ch, cj - ch

            ci_j, ci_h, ci_gap = stats.bootstrap_intervals(n, length_stats)
            _put_ci(report, "length_corr_judge", ci_j)
            _put_ci(report, "length_corr_human", ci_h)
            _put_ci(report, "length_bias", ci_gap)
    rate, n_decided = flip_rate(pairs)
    report["n_pairs"] = len(pairs)
    report["n_pairs_decided"] = n_decided
    report["flip_rate"] = rate
    if rate is not None:
        _put_ci(report, "flip_rate", stats.wilson_ci(round(rate * n_decided), n_decided))
    return report


def _put_ci(report: dict[str, Any], key: str, ci: tuple[float, float]) -> None:
    """Store a 95% interval next to its estimate as ``<key>_ci_low`` / ``<key>_ci_high``."""
    report[f"{key}_ci_low"], report[f"{key}_ci_high"] = ci


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
        if v is None:
            return "—"
        lo, hi = report.get(f"{key}_ci_low"), report.get(f"{key}_ci_high")
        ci = "" if lo is None or hi is None else f" (95% {lo:.{digits}f} to {hi:.{digits}f})"
        return f"{v:.{digits}f}{ci}"

    return "\n".join(
        [
            f"Judge reliability vs human label `{report['human_key']}`",
            f"  items labelled: {report['n_labelled']}, judged: {report['n_judged']}, "
            f"judge failures: {report['n_judge_failures']}",
            f"  Cohen's kappa: {f('kappa')}",
            f"  weighted kappa: {f('weighted_kappa')}",
            f"  pass/fail kappa: {f('pass_fail_kappa')}",
            f"  exact agreement: {f('exact_agreement')}   "
            f"mean |judge − human|: {f('mean_abs_diff')}",
            f"  length bias (Spearman with output words): judge {f('length_corr_judge')}, "
            f"human {f('length_corr_human')}; judge minus human {f('length_bias')}",
            f"  position-swap flip rate: {f('flip_rate')} over {report['n_pairs_decided']} decided "
            f"pairs ({report['n_pairs']} judged in both orders)",
        ]
    )
