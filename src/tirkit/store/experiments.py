"""Experiments (eval runs) and their per-item results."""

from __future__ import annotations

import time
from typing import Any

from tirkit.store.base import StoreBase
from tirkit.types import RunResult


class ExperimentsMixin(StoreBase):
    def save_experiment(self, result: Any) -> None:
        """Persist a :class:`tirkit.runner.RunResult` (outputs, per-item scores and metadata)."""
        import uuid as _uuid

        q = self.dialect.q
        config = {
            **(result.config or {}),
            "scorer_info": result.scorer_info,
        }
        with self.tx() as cur:
            cur.execute(
                q(
                    "INSERT INTO experiments (project_id, experiment_id, name, dataset, version, "
                    "target, prompt_version, config, status, created, finished) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (project_id, experiment_id) DO UPDATE SET "
                    "status = excluded.status, finished = excluded.finished, "
                    "config = excluded.config"
                ),
                (
                    self.project_id,
                    result.experiment_id,
                    result.name,
                    result.dataset,
                    result.dataset_version,
                    result.target,
                    (result.config or {}).get("prompt_version"),
                    self._json(config),
                    "finished",
                    result.created,
                    time.time(),
                ),
            )
            n = len(result.outputs)

            def at(seq: list[Any], i: int) -> Any:
                return seq[i] if i < len(seq) else None

            cur.executemany(
                q(
                    "INSERT INTO experiment_items (project_id, experiment_id, idx, trace_id, "
                    "output, "
                    "latency_ms, cost_usd, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (project_id, experiment_id, idx) DO UPDATE SET "
                    "output = excluded.output, latency_ms = excluded.latency_ms, "
                    "cost_usd = excluded.cost_usd, error = excluded.error"
                ),
                [
                    (
                        self.project_id,
                        result.experiment_id,
                        i,
                        at(result.trace_ids, i),
                        self._json(result.outputs[i]),
                        at(result.latencies_ms, i),
                        at(result.costs_usd, i),
                        at(result.errors, i),
                    )
                    for i in range(n)
                ],
            )
            cur.execute(
                q("DELETE FROM scores WHERE project_id = ? AND experiment_id = ?"),
                (self.project_id, result.experiment_id),
            )
            rows = []
            now = time.time()
            for scorer, values in result.scores.items():
                reasons = result.reasons.get(scorer, [])
                version = result.scorer_info.get(scorer, {}).get("version")
                for i, value in enumerate(values):
                    reason = at(reasons, i)
                    rows.append(
                        (
                            self.project_id,
                            _uuid.uuid4().hex,
                            at(result.trace_ids, i),
                            result.experiment_id,
                            i,
                            scorer,
                            value,
                            "experiment",
                            version,
                            reason if value is not None else None,
                            reason if value is None else None,
                            now,
                        )
                    )
            cur.executemany(
                q(
                    "INSERT INTO scores (project_id, score_id, trace_id, experiment_id, item_idx, "
                    "name, value, source, scorer_version, reason, error, created) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                rows,
            )

    def load_experiment(self, experiment_id: str) -> Any:
        rows = self.execute(
            "SELECT name, dataset, version, target, config, created FROM experiments "
            "WHERE project_id = ? AND experiment_id = ?",
            [self.project_id, experiment_id],
        )
        if not rows:
            raise KeyError(f"no experiment {experiment_id!r}")
        name, dataset, version, target, config, created = rows[0]
        config = self.dialect.load_json(config) or {}
        scorer_info = config.pop("scorer_info", {})
        items = self.execute(
            "SELECT idx, trace_id, output, latency_ms, cost_usd, error FROM experiment_items "
            "WHERE project_id = ? AND experiment_id = ? ORDER BY idx",
            [self.project_id, experiment_id],
        )
        n = len(items)
        result = RunResult(
            dataset=dataset,
            dataset_version=version,
            experiment_id=experiment_id,
            name=name,
            target=target,
            created=created,
            config=config,
            scorer_info=scorer_info,
            outputs=[self.dialect.load_json(r[2]) for r in items],
            trace_ids=[r[1] for r in items],
            latencies_ms=[r[3] for r in items],
            costs_usd=[r[4] for r in items],
            errors=[r[5] for r in items],
        )
        for scorer, idx, value, reason, error in self.execute(
            "SELECT name, item_idx, value, reason, error FROM scores "
            "WHERE project_id = ? AND experiment_id = ? ORDER BY name, item_idx",
            [self.project_id, experiment_id],
        ):
            result.scores.setdefault(scorer, [None] * n)[idx] = value
            result.reasons.setdefault(scorer, [None] * n)[idx] = reason or error
        return result

    def list_experiments(
        self, dataset: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT experiment_id, name, dataset, version, target, status, created, finished "
            "FROM experiments WHERE project_id = ?"
        )
        params: list[Any] = [self.project_id]
        if dataset:
            sql += " AND dataset = ?"
            params.append(dataset)
        rows = self.execute(sql + " ORDER BY created DESC LIMIT ?", [*params, limit])
        keys = ["experiment_id", "name", "dataset", "version", "target", "status", "created"]
        return [dict(zip([*keys, "finished"], r, strict=True)) for r in rows]
