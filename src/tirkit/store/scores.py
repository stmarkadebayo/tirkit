"""Scores attached to traces and experiment items."""

from __future__ import annotations

import time
from typing import Any

from tirkit.store.base import StoreBase


class ScoresMixin(StoreBase):
    def save_score(
        self,
        name: str,
        value: float | None,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
        value_str: str | None = None,
        source: str = "api",
        reason: str | None = None,
        error: str | None = None,
        author: str | None = None,
        scorer_version: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Attach a score to a trace or span (human labels, user feedback, online evals)."""
        import uuid as _uuid

        score_id = _uuid.uuid4().hex
        self.execute(
            "INSERT INTO scores (project_id, score_id, trace_id, span_id, name, value, value_str, "
            "source, scorer_version, reason, error, author, created, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                self.project_id,
                score_id,
                trace_id,
                span_id,
                name,
                value,
                value_str,
                source,
                scorer_version,
                reason,
                error,
                author,
                time.time(),
                self._json(metadata),
            ],
        )
        return score_id

    def query_scores(
        self,
        *,
        trace_id: str | None = None,
        name: str | None = None,
        source: str | None = None,
        experiment_id: str | None = None,
        since: float | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT score_id, trace_id, span_id, experiment_id, item_idx, name, value, value_str, "
            "source, scorer_version, reason, error, author, created FROM scores "
            "WHERE project_id = ?"
        )
        params: list[Any] = [self.project_id]
        for col, val in (
            ("trace_id", trace_id),
            ("name", name),
            ("source", source),
            ("experiment_id", experiment_id),
        ):
            if val is not None:
                sql += f" AND {col} = ?"
                params.append(val)
        if since is not None:
            sql += " AND created >= ?"
            params.append(since)
        rows = self.execute(sql + " ORDER BY created DESC LIMIT ?", [*params, limit])
        keys = (
            "score_id trace_id span_id experiment_id item_idx name value value_str source "
            "scorer_version reason error author created"
        ).split()
        return [dict(zip(keys, r, strict=True)) for r in rows]
