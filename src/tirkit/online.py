"""Online evals: score sampled production traces as they arrive (SPEC FR-17, §6.7).

An evaluator is a trace filter, a sampling rate and a scorer (a rule scorer, or an LLM judge on a
server-configured model). The worker processes new traces after each evaluator's cursor. Sampling
is deterministic: a trace is scored if ``sha256(trace_id:evaluator_id) mod 10,000`` is below
``rate × 10,000``. Judge spend is capped per project per day; when the budget is spent, traces are
skipped and counted, never queued indefinitely.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from tirkit.datasets import Example
from tirkit.store import SQLStore
from tirkit.tracing import sink_context

log = logging.getLogger("tirkit.online")

BATCH = 50
SETTLE_S = 5.0  # wait for late child spans before scoring a trace


def sampled(trace_id: str, evaluator_id: str, rate: float) -> bool:
    h = int(hashlib.sha256(f"{trace_id}:{evaluator_id}".encode()).hexdigest(), 16)
    return h % 10_000 < rate * 10_000


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def spend_today(store: SQLStore) -> float:
    rows = store.execute(
        "SELECT usd FROM judge_spend WHERE project_id = ? AND day = ?", [store.project_id, _today()]
    )
    return float(rows[0][0]) if rows else 0.0


def add_spend(store: SQLStore, usd: float) -> None:
    if not usd:
        return
    store.execute(
        "INSERT INTO judge_spend (project_id, day, usd) VALUES (?, ?, ?) "
        "ON CONFLICT (project_id, day) DO UPDATE SET usd = judge_spend.usd + excluded.usd",
        [store.project_id, _today(), usd],
    )


class Evaluators:
    def __init__(self, store: SQLStore) -> None:
        self.store = store
        self.pid = store.project_id

    def create(
        self,
        name: str,
        scorer: dict[str, Any],
        *,
        sampling_rate: float = 0.1,
        filter: dict[str, Any] | None = None,
        backfill_s: float = 0.0,
    ) -> str:
        if not 0 < sampling_rate <= 1:
            raise ValueError("sampling_rate must be in (0, 1]")
        spec = scorer.get("spec")
        if not spec:
            raise ValueError("scorer.spec required (e.g. judge:correctness, json_valid)")
        if spec.startswith("judge:") and not scorer.get("model"):
            raise ValueError("judge evaluators need scorer.model")
        allowed = {"name", "release", "user_id", "session_id", "tag", "error"}
        if set(filter or {}) - allowed:
            raise ValueError(f"filter keys must be among {sorted(allowed)}")
        eid = "ev_" + uuid.uuid4().hex[:10]
        self.store.execute(
            "INSERT INTO evaluators (project_id, evaluator_id, name, filter, sampling_rate, "
            "scorer, active, cursor, created) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            [
                self.pid,
                eid,
                name,
                self.store._json(filter or {}),
                sampling_rate,
                self.store._json(scorer),
                time.time() - backfill_s,
                time.time(),
            ],
        )
        return eid

    def items(self) -> list[dict[str, Any]]:
        rows = self.store.execute(
            "SELECT evaluator_id, name, filter, sampling_rate, scorer, active, cursor, n_scored, "
            "n_failed, n_skipped_budget, n_not_sampled, created FROM evaluators "
            "WHERE project_id = ? ORDER BY created",
            [self.pid],
        )
        keys = (
            "evaluator_id name filter sampling_rate scorer active cursor n_scored n_failed "
            "n_skipped_budget n_not_sampled created"
        ).split()
        out = []
        for r in rows:
            d = dict(zip(keys, r, strict=True))
            d["filter"] = self.store.dialect.load_json(d["filter"]) or {}
            d["scorer"] = self.store.dialect.load_json(d["scorer"]) or {}
            d["active"] = bool(d["active"])
            out.append(d)
        return out

    def set_active(self, evaluator_id: str, active: bool) -> None:
        self.store.execute(
            "UPDATE evaluators SET active = ? WHERE project_id = ? AND evaluator_id = ?",
            [int(active), self.pid, evaluator_id],
        )

    def delete(self, evaluator_id: str) -> None:
        self.store.execute(
            "DELETE FROM evaluators WHERE project_id = ? AND evaluator_id = ?",
            [self.pid, evaluator_id],
        )

    def _scorer(self, cfg: dict[str, Any]) -> Any:
        from tirkit.worker_tasks import build_scorers

        spec = str(cfg["spec"])
        if spec.startswith("judge:") and cfg.get("rubric_text"):
            from tirkit.judge import LLMJudge, Rubric
            from tirkit.providers import ServerJudgeModel

            rubric = Rubric(spec.split(":", 1)[1], str(cfg["rubric_text"]))
            return LLMJudge(ServerJudgeModel(str(cfg["model"])), rubric, cache=self.store)
        return build_scorers([spec], self.store, cfg.get("model"))[0]

    def process(self, evaluator: dict[str, Any], budget_usd: float | None) -> dict[str, int]:
        """Score the next batch of traces after this evaluator's cursor."""
        from tirkit.judge import BudgetExceeded
        from tirkit.scorers import higher_is_better, scorer_version

        now = time.time()
        where = {
            **evaluator["filter"],
            "start_after": evaluator["cursor"],
            "start_before": now - SETTLE_S,
        }
        traces = self.store.query_traces(where, limit=BATCH)
        traces.sort(key=lambda t: t.start or 0)
        stats = {"scored": 0, "failed": 0, "skipped_budget": 0, "not_sampled": 0}
        if not traces:
            return stats
        is_judge = str(evaluator["scorer"]["spec"]).startswith("judge:")
        scorer = self._scorer(evaluator["scorer"])
        cursor = evaluator["cursor"]
        for t in traces:
            cursor = max(cursor, (t.start or cursor) + 1e-6)
            if not sampled(t.trace_id, evaluator["evaluator_id"], evaluator["sampling_rate"]):
                stats["not_sampled"] += 1
                continue
            if is_judge and budget_usd is not None and spend_today(self.store) >= budget_usd:
                stats["skipped_budget"] += 1
                continue
            inp = t.input if isinstance(t.input, dict) else {"input": t.input}
            example = Example(inp)
            try:
                with sink_context(self.store):
                    sc = scorer.score(example, t.output)
            except BudgetExceeded:
                stats["skipped_budget"] += 1
                continue
            cost = getattr(getattr(scorer, "complete", None), "last_cost", None)
            add_spend(self.store, cost or 0.0)
            self.store.save_score(
                evaluator["name"],
                sc.value if sc.ok else None,
                trace_id=t.trace_id,
                source="llm_judge" if is_judge else "rule",
                reason=sc.reason if sc.ok else None,
                error=None if sc.ok else sc.error,
                scorer_version=scorer_version(scorer),
                metadata={
                    "evaluator_id": evaluator["evaluator_id"],
                    "higher_is_better": higher_is_better(scorer),
                },
            )
            stats["scored" if sc.ok else "failed"] += 1
        self.store.execute(
            "UPDATE evaluators SET cursor = ?, n_scored = n_scored + ?, n_failed = n_failed + ?, "
            "n_skipped_budget = n_skipped_budget + ?, n_not_sampled = n_not_sampled + ? "
            "WHERE project_id = ? AND evaluator_id = ?",
            [
                cursor,
                stats["scored"],
                stats["failed"],
                stats["skipped_budget"],
                stats["not_sampled"],
                self.pid,
                evaluator["evaluator_id"],
            ],
        )
        return stats


def run_all(root: SQLStore) -> None:
    """Periodic task: process every active evaluator in every project."""
    rows = root.execute(
        "SELECT DISTINCT e.project_id, p.settings FROM evaluators e LEFT JOIN projects p "
        "ON p.project_id = e.project_id WHERE e.active = 1"
    )
    for pid, settings in rows:
        budget = (root.dialect.load_json(settings) or {}).get("judge_budget_usd_per_day")
        ev = Evaluators(root.for_project(pid))
        for e in ev.items():
            if not e["active"]:
                continue
            try:
                ev.process(e, float(budget) if budget is not None else None)
            except Exception:  # noqa: BLE001
                log.warning("evaluator %s failed", e["evaluator_id"], exc_info=True)
