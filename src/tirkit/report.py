"""Markdown and static HTML reports: runs, comparisons and gate results (PR comments)."""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import Any

from tirkit.runner import RunResult, ScorerComparison


def _f(x: float | None, digits: int = 3) -> str:
    return "—" if x is None else f"{x:.{digits}f}"


def _ci(ci: tuple[float, float] | None, digits: int = 3) -> str:
    return "—" if ci is None else f"[{ci[0]:.{digits}f}, {ci[1]:.{digits}f}]"


def render_run(run: RunResult, *, seed: int = 0) -> str:
    """Markdown summary of one run: per-scorer mean with 95% CI, failures, latency and cost."""
    lines = [
        f"### Run `{run.experiment_id}`" + (f" — {run.name}" if run.name else ""),
        "",
        f"Dataset `{run.dataset}@{run.dataset_version}`, {len(run.outputs)} examples, "
        f"target `{run.target}`.",
        "",
        "| Scorer | Mean | 95% CI | Scored | Failed |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, row in run.summary(seed=seed).items():
        arrow = "" if run.higher_is_better(name) else " (lower is better)"
        lines.append(
            f"| {name}{arrow} | {_f(row.get('mean'))} | {_ci(row.get('ci'))} | {row['n']} | "
            f"{row['failed']} |"
        )
    errors = sum(e is not None for e in run.errors)
    lat = [x for x in run.latencies_ms if x is not None]
    cost = [x for x in run.costs_usd if x is not None]
    lines += [
        "",
        f"Target errors: {errors}. "
        + (f"Median latency: {sorted(lat)[len(lat) // 2]:.0f} ms. " if lat else "")
        + (f"Total cost: ${sum(cost):.4f}." if cost else ""),
    ]
    return "\n".join(lines)


def render_compare(
    comparisons: Sequence[ScorerComparison],
    baseline: RunResult,
    candidate: RunResult,
    *,
    gate_scorer: str | None = None,
    passed: bool | None = None,
    max_drop: float = 0.0,
) -> str:
    """Markdown comparison table; with a gate result it is ready to post as a PR comment."""
    lines = []
    if passed is not None:
        icon = "✅" if passed else "❌"
        state = "passed" if passed else "failed"
        lines += [
            f"## {icon} Tirkit eval gate {state}",
            "",
            f"Gate scorer `{gate_scorer}`, allowed drop {max_drop}. The gate fails only when the "
            "candidate is confidently worse: the upper end of the 95% CI of the paired "
            "difference is below the allowed drop.",
            "",
        ]
    lines += [
        f"Baseline `{baseline.experiment_id}` vs candidate `{candidate.experiment_id}` on "
        f"`{baseline.dataset}@{baseline.dataset_version}` (paired, 95% bootstrap CIs).",
        "",
        "| Scorer | Baseline | Candidate | Difference (cand − base) | 95% CI of difference | "
        "Verdict | n |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in comparisons:
        sign = "+" if c.diff >= 0 else ""
        note = "" if c.higher_is_better else " ↓"
        lines.append(
            f"| {c.scorer}{note} | {c.mean_baseline:.3f} {_ci(c.ci_baseline)} | "
            f"{c.mean_candidate:.3f} {_ci(c.ci_candidate)} | {sign}{c.diff:.3f} | "
            f"{_ci(c.diff_ci)} | {c.verdict} | {c.n}"
            + (f" ({c.excluded} excluded)" if c.excluded else "")
            + " |"
        )
    regressions = _worst_items(comparisons, baseline, candidate)
    if regressions:
        lines += ["", "<details><summary>Largest per-item regressions</summary>", ""]
        lines += ["| Scorer | Item | Baseline | Candidate |", "| --- | --- | --- | --- |"]
        lines += [f"| {s} | {i} | {a:.3f} | {b:.3f} |" for s, i, a, b in regressions]
        lines += ["", "</details>"]
    return "\n".join(lines)


def _worst_items(
    comparisons: Sequence[ScorerComparison], baseline: RunResult, candidate: RunResult, k: int = 10
) -> list[tuple[str, int, float, float]]:
    rows = []
    for c in comparisons:
        sign = 1 if c.higher_is_better else -1
        for i, (a, b) in enumerate(
            zip(baseline.scores[c.scorer], candidate.scores[c.scorer], strict=True)
        ):
            if a is not None and b is not None and sign * (b - a) < 0:
                rows.append((sign * (b - a), c.scorer, i, a, b))
    rows.sort()
    return [(s, i, a, b) for _, s, i, a, b in rows[:k]]


def markdown_to_html(md: str, title: str = "Tirkit report") -> str:
    """Tiny Markdown-table renderer for a static, dependency-free HTML report."""
    out = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{html.escape(title)}</title>",
        "<style>body{font:14px/1.5 system-ui,sans-serif;max-width:1100px;margin:2em auto;"
        "padding:0 16px}table{border-collapse:collapse;margin:1em 0}td,th{border:1px solid #ccc;"
        "padding:4px 8px;text-align:left}code{background:#f4f4f4;padding:0 3px}</style>",
        "</head><body>",
    ]
    in_table = False
    for line in md.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= {"-", " ", ":"} for c in cells):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{level}>{_inline(line[level:].strip())}</h{level}>")
        elif line.strip().startswith("<"):
            out.append(line)
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    if in_table:
        out.append("</table>")
    out.append("</body></html>")
    return "\n".join(out)


def _inline(text: str) -> str:
    import re

    esc = html.escape(text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)


def compare_json(comparisons: Sequence[ScorerComparison]) -> list[dict[str, Any]]:
    return [
        {
            "scorer": c.scorer,
            "n": c.n,
            "excluded": c.excluded,
            "mean_baseline": c.mean_baseline,
            "mean_candidate": c.mean_candidate,
            "ci_baseline": list(c.ci_baseline),
            "ci_candidate": list(c.ci_candidate),
            "diff": c.diff,
            "diff_ci": list(c.diff_ci),
            "higher_is_better": c.higher_is_better,
            "verdict": c.verdict,
        }
        for c in comparisons
    ]
