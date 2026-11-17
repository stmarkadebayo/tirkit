"""Human annotation: score configs and annotation queues (SPEC FR-18).

A score config says what can be scored (numeric range, categories, or boolean). A queue holds
traces for review; reviewers take the next pending item (locked to them for 10 minutes), submit
scores for the queue's configs, or skip. Labels are stored as ``source="human"`` scores and can be
exported as JSONL for the judge-reliability report.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from tirkit.store import SQLStore

LOCK_S = 600


class Annotation:
    def __init__(self, store: SQLStore) -> None:
        self.store = store
        self.pid = store.project_id

    # -- score configs -------------------------------------------------------------------------

    def save_config(
        self,
        name: str,
        data_type: str,
        *,
        min_value: float | None = None,
        max_value: float | None = None,
        categories: list[str] | None = None,
        description: str | None = None,
    ) -> None:
        if data_type not in {"numeric", "categorical", "boolean"}:
            raise ValueError("data_type must be numeric, categorical or boolean")
        if data_type == "categorical" and not categories:
            raise ValueError("categorical configs need categories")
        if data_type == "numeric" and (
            min_value is None or max_value is None or min_value >= max_value
        ):
            raise ValueError("numeric configs need min_value < max_value")
        self.store.execute(
            "INSERT INTO score_configs (project_id, name, data_type, min_value, max_value, "
            "categories, description, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (project_id, name) DO UPDATE SET data_type = excluded.data_type, "
            "min_value = excluded.min_value, max_value = excluded.max_value, "
            "categories = excluded.categories, description = excluded.description",
            [
                self.pid,
                name,
                data_type,
                min_value,
                max_value,
                self.store._json(categories),
                description,
                time.time(),
            ],
        )

    def configs(self) -> list[dict[str, Any]]:
        rows = self.store.execute(
            "SELECT name, data_type, min_value, max_value, categories, description FROM "
            "score_configs WHERE project_id = ? ORDER BY name",
            [self.pid],
        )
        return [
            {
                "name": r[0],
                "data_type": r[1],
                "min_value": r[2],
                "max_value": r[3],
                "categories": self.store.dialect.load_json(r[4]),
                "description": r[5],
            }
            for r in rows
        ]

    def validate(self, name: str, value: Any) -> tuple[float | None, str | None]:
        """Check a submitted value against its config; returns (numeric value, category)."""
        cfg = next((c for c in self.configs() if c["name"] == name), None)
        if cfg is None:
            raise ValueError(f"no score config {name!r}")
        if cfg["data_type"] == "numeric":
            v = float(value)
            if not cfg["min_value"] <= v <= cfg["max_value"]:
                raise ValueError(
                    f"{name} must be between {cfg['min_value']} and {cfg['max_value']}"
                )
            return v, None
        if cfg["data_type"] == "boolean":
            if value not in (True, False, 0, 1, "true", "false"):
                raise ValueError(f"{name} must be true or false")
            return float(value in (True, 1, "true")), None
        if value not in cfg["categories"]:
            raise ValueError(f"{name} must be one of {cfg['categories']}")
        return float(cfg["categories"].index(value)), str(value)

    # -- queues --------------------------------------------------------------------------------

    def create_queue(
        self, name: str, score_names: list[str], description: str | None = None
    ) -> str:
        known = {c["name"] for c in self.configs()}
        missing = set(score_names) - known
        if missing or not score_names:
            raise ValueError(
                f"unknown score configs: {sorted(missing)}" if missing else "pick scores"
            )
        qid = "q_" + uuid.uuid4().hex[:10]
        self.store.execute(
            "INSERT INTO annotation_queues (project_id, queue_id, name, description, score_names, "
            "created) VALUES (?, ?, ?, ?, ?, ?)",
            [self.pid, qid, name, description, self.store._json(score_names), time.time()],
        )
        return qid

    def queues(self) -> list[dict[str, Any]]:
        rows = self.store.execute(
            "SELECT q.queue_id, q.name, q.description, q.score_names, q.created, "
            "(SELECT COUNT(*) FROM queue_items i WHERE i.project_id = q.project_id AND "
            "i.queue_id = q.queue_id), "
            "(SELECT COUNT(*) FROM queue_items i WHERE i.project_id = q.project_id AND "
            "i.queue_id = q.queue_id AND i.status = 'done') "
            "FROM annotation_queues q WHERE q.project_id = ? ORDER BY q.created DESC",
            [self.pid],
        )
        return [
            {
                "queue_id": r[0],
                "name": r[1],
                "description": r[2],
                "score_names": self.store.dialect.load_json(r[3]),
                "created": r[4],
                "n_items": int(r[5]),
                "n_done": int(r[6]),
            }
            for r in rows
        ]

    def queue(self, queue_id: str) -> dict[str, Any]:
        found = [q for q in self.queues() if q["queue_id"] == queue_id]
        if not found:
            raise KeyError(f"no queue {queue_id!r}")
        return found[0]

    def add_items(self, queue_id: str, trace_ids: list[str]) -> int:
        self.queue(queue_id)
        existing = {
            r[0]
            for r in self.store.execute(
                "SELECT trace_id FROM queue_items WHERE project_id = ? AND queue_id = ?",
                [self.pid, queue_id],
            )
        }
        new = [t for t in dict.fromkeys(trace_ids) if t not in existing]
        now = time.time()
        with self.store.tx() as cur:
            cur.executemany(
                self.store.dialect.q(
                    "INSERT INTO queue_items (project_id, queue_id, item_id, trace_id, status, "
                    "created) VALUES (?, ?, ?, ?, 'pending', ?)"
                ),
                [
                    (self.pid, queue_id, uuid.uuid4().hex[:12], t, now + i * 1e-6)
                    for i, t in enumerate(new)
                ],
            )
        return len(new)

    def next_item(self, queue_id: str, reviewer: str) -> dict[str, Any] | None:
        """Oldest pending item not locked by someone else; locks it for this reviewer."""
        now = time.time()
        rows = self.store.execute(
            "SELECT item_id, trace_id FROM queue_items WHERE project_id = ? AND queue_id = ? "
            "AND status = 'pending' AND (locked_by IS NULL OR locked_by = ? OR locked_until < ?) "
            "ORDER BY CASE WHEN locked_by = ? THEN 0 ELSE 1 END, created LIMIT 1",
            [self.pid, queue_id, reviewer, now, reviewer],
        )
        if not rows:
            return None
        item_id, trace_id = rows[0]
        self.store.execute(
            "UPDATE queue_items SET locked_by = ?, locked_until = ? WHERE project_id = ? AND "
            "queue_id = ? AND item_id = ?",
            [reviewer, now + LOCK_S, self.pid, queue_id, item_id],
        )
        return {"item_id": item_id, "trace_id": trace_id}

    def complete(
        self,
        queue_id: str,
        item_id: str,
        reviewer: str,
        scores: dict[str, Any],
        comment: str | None = None,
    ) -> list[str]:
        q = self.queue(queue_id)
        rows = self.store.execute(
            "SELECT trace_id, status FROM queue_items WHERE project_id = ? AND queue_id = ? "
            "AND item_id = ?",
            [self.pid, queue_id, item_id],
        )
        if not rows:
            raise KeyError("no such item")
        trace_id = rows[0][0]
        missing = set(q["score_names"]) - set(scores)
        if missing:
            raise ValueError(f"missing scores: {sorted(missing)}")
        checked = {name: self.validate(name, scores[name]) for name in q["score_names"]}
        ids = [
            self.store.save_score(
                name,
                value,
                trace_id=trace_id,
                value_str=category,
                source="human",
                author=reviewer,
                reason=comment,
                metadata={"queue_id": queue_id, "item_id": item_id},
            )
            for name, (value, category) in checked.items()
        ]
        self.store.execute(
            "UPDATE queue_items SET status = 'done', completed_by = ?, completed_at = ?, "
            "locked_by = NULL WHERE project_id = ? AND queue_id = ? AND item_id = ?",
            [reviewer, time.time(), self.pid, queue_id, item_id],
        )
        return ids

    def skip(self, queue_id: str, item_id: str, reviewer: str) -> None:
        self.store.execute(
            "UPDATE queue_items SET status = 'skipped', completed_by = ?, completed_at = ?, "
            "locked_by = NULL WHERE project_id = ? AND queue_id = ? AND item_id = ?",
            [reviewer, time.time(), self.pid, queue_id, item_id],
        )

    def labels(self, queue_id: str) -> list[dict[str, Any]]:
        """Completed items with their trace input/output and human scores (for judge reports)."""
        q = self.queue(queue_id)
        rows = self.store.execute(
            "SELECT item_id, trace_id, completed_by, completed_at FROM queue_items WHERE "
            "project_id = ? AND queue_id = ? AND status = 'done' ORDER BY completed_at",
            [self.pid, queue_id],
        )
        out = []
        for item_id, trace_id, by, at in rows:
            trace = self.store.get_trace(trace_id)
            scores = {
                s["name"]: s["value_str"] if s["value_str"] is not None else s["value"]
                for s in reversed(self.store.query_scores(trace_id=trace_id, source="human"))
                if s["name"] in q["score_names"]
            }
            out.append(
                {
                    "item_id": item_id,
                    "trace_id": trace_id,
                    "input": trace.input if trace else None,
                    "output": trace.output if trace else None,
                    "labels": scores,
                    "labeller": by,
                    "labelled_at": at,
                }
            )
        return out
