"""Spans and traces: writes, trace aggregation, filters, search and retention."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from tirkit.store.base import StoreBase
from tirkit.types import Span, Trace

_WORD = re.compile(r"\w+", re.UNICODE)

SPAN_COLUMNS = (
    "project_id span_id trace_id parent_id name kind status level start_time end_time duration_ms "
    "ttft_ms model model_params input output usage input_tokens output_tokens cost_usd error "
    "session_id user_id release tags prompt_version attributes"
).split()

_SPAN_JSON = {"model_params", "input", "output", "usage", "tags", "attributes"}

# Span filters: key -> (column, operator)
_SPAN_FILTERS = {
    "trace_id": ("trace_id", "="),
    "parent_id": ("parent_id", "="),
    "name": ("name", "="),
    "kind": ("kind", "="),
    "model": ("model", "="),
    "status": ("status", "="),
    "session_id": ("session_id", "="),
    "user_id": ("user_id", "="),
    "release": ("release", "="),
    "prompt_version": ("prompt_version", "="),
    "start_after": ("start_time", ">="),
    "start_before": ("start_time", "<"),
    "min_cost": ("cost_usd", ">="),
    "min_duration_ms": ("duration_ms", ">="),
}

_TRACE_FILTERS = {
    "trace_id": ("trace_id", "="),
    "name": ("name", "="),
    "session_id": ("session_id", "="),
    "user_id": ("user_id", "="),
    "release": ("release", "="),
    "start_after": ("start_time", ">="),
    "start_before": ("start_time", "<"),
    "min_cost": ("cost_usd", ">="),
    "min_duration_ms": ("duration_ms", ">="),
}


class SpansMixin(StoreBase):
    _TRACE_SUMMARY_COLS = (
        "project_id, trace_id, name, session_id, user_id, release, tags, input, output, "
        "start_time, end_time, duration_ms, n_spans, input_tokens, output_tokens, cost_usd, "
        "has_error"
    )

    _TRACE_COLS = (
        "trace_id name session_id user_id release tags input output start_time end_time "
        "duration_ms n_spans input_tokens output_tokens cost_usd has_error"
    ).split()

    def save_span(self, span: Span) -> None:
        self.save_spans([span])

    def save_spans(self, spans: Iterable[Span]) -> None:
        spans = list(spans)
        if not spans:
            return
        cols = ", ".join(SPAN_COLUMNS)
        marks = ", ".join("?" for _ in SPAN_COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in SPAN_COLUMNS[2:])
        sql = self.dialect.q(
            f"INSERT INTO spans ({cols}) VALUES ({marks}) "
            f"ON CONFLICT (project_id, span_id) DO UPDATE SET {updates}"
        )
        rows = [self._span_row(s) for s in spans]
        with self.tx() as cur:
            cur.executemany(sql, rows)
            trace_ids = list(dict.fromkeys(s.trace_id for s in spans))
            for i in range(0, len(trace_ids), 500):
                self._refresh_traces(cur, trace_ids[i : i + 500])

    def _refresh_traces(self, cur: Any, trace_ids: list[str]) -> None:
        """Recompute trace summaries from their spans in one statement (idempotent under
        re-delivery): totals over all spans, descriptive fields from the root span (or the
        earliest span while the root has not arrived)."""
        marks = ", ".join("?" for _ in trace_ids)
        names = [c.strip() for c in self._TRACE_SUMMARY_COLS.split(",")]
        updates = ", ".join(f"{c} = excluded.{c}" for c in names[2:])
        sql = (
            "WITH s AS (SELECT project_id, trace_id, name, session_id, user_id, release, tags, "
            "input, output, "
            "ROW_NUMBER() OVER (PARTITION BY trace_id ORDER BY "
            "CASE WHEN parent_id IS NULL THEN 0 ELSE 1 END, start_time) AS rn, "
            "MIN(start_time) OVER w AS t_start, MAX(end_time) OVER w AS t_end, "
            "COUNT(*) OVER w AS n, SUM(input_tokens) OVER w AS tin, "
            "SUM(output_tokens) OVER w AS tout, SUM(cost_usd) OVER w AS cost, "
            "MAX(CASE WHEN status = 'error' THEN 1 ELSE 0 END) OVER w AS err "
            f"FROM spans WHERE project_id = ? AND trace_id IN ({marks}) "
            "WINDOW w AS (PARTITION BY trace_id)) "
            f"INSERT INTO traces ({self._TRACE_SUMMARY_COLS}) "
            "SELECT project_id, trace_id, name, session_id, user_id, release, tags, input, "
            "output, t_start, t_end, (t_end - t_start) * 1000, n, tin, tout, cost, err "
            "FROM s WHERE rn = 1 "
            f"ON CONFLICT (project_id, trace_id) DO UPDATE SET {updates}"
        )
        cur.execute(self.dialect.q(sql), (self.project_id, *trace_ids))

    def _span_row(self, s: Span) -> tuple[Any, ...]:
        values = {
            "project_id": self.project_id,
            "span_id": s.span_id,
            "trace_id": s.trace_id,
            "parent_id": s.parent_id,
            "name": s.name,
            "kind": s.kind,
            "status": s.status,
            "level": s.level,
            "start_time": s.start,
            "end_time": s.end,
            "duration_ms": s.duration_ms,
            "ttft_ms": s.ttft_ms,
            "model": s.model,
            "model_params": s.model_params or None,
            "input": s.input,
            "output": s.output,
            "usage": s.usage or None,
            "input_tokens": s.input_tokens,
            "output_tokens": s.output_tokens,
            "cost_usd": s.cost_usd,
            "error": s.error,
            "session_id": s.session_id,
            "user_id": s.user_id,
            "release": s.release,
            "tags": s.tags or None,
            "prompt_version": s.prompt_version,
            "attributes": s.attributes or None,
        }
        return tuple(self._json(values[c]) if c in _SPAN_JSON else values[c] for c in SPAN_COLUMNS)

    def _where(
        self, where: dict[str, Any] | None, filters: dict[str, tuple[str, str]], table: str
    ) -> tuple[str, list[Any]]:
        clauses = ["project_id = ?"]
        params: list[Any] = [self.project_id]
        d = self.dialect
        for key, value in (where or {}).items():
            if value is None:
                continue
            if key in filters:
                col, op = filters[key]
                clauses.append(f"{col} {op} ?")
                params.append(value)
            elif key == "error":
                if table == "spans":
                    clauses.append("status = 'error'" if value else "status <> 'error'")
                else:
                    clauses.append("has_error = 1" if value else "has_error = 0")
            elif key == "tag":
                clauses.append(d.tag_contains("tags"))
                params.append(value)
            elif key == "root" and table == "spans":
                clauses.append("parent_id IS NULL" if value else "parent_id IS NOT NULL")
            elif key == "search":
                clause, extra = self._search_clause(str(value), table)
                clauses.append(clause)
                params.extend(extra)
            elif key == "attributes" and table == "spans":
                for akey, aval in dict(value).items():
                    clauses.append(f"{d.json_get('attributes', str(akey))} = ?")
                    params.append(
                        str(aval) if d.name == "postgres" and not isinstance(aval, str) else aval
                    )
            else:
                raise ValueError(f"unknown filter: {key}")
        return " AND ".join(clauses), params

    def _search_clause(self, text: str, table: str) -> tuple[str, list[Any]]:
        """Indexed word-prefix search (every word must appear); falls back to a substring
        match when the query has no word characters. On traces, any span of the trace counts."""
        words = _WORD.findall(text.lower())[:12]
        d = self.dialect
        if not words:
            op = d.like()
            pat = f"%{text}%"
            span_match = (
                f"({d.json_text('input')} {op} ? OR {d.json_text('output')} {op} ? OR name {op} ?)"
            )
            span_params: list[Any] = [pat, pat, pat]
        elif d.name == "postgres":
            span_match = "search_tsv @@ to_tsquery('simple', ?)"
            span_params = [" & ".join(f"{w}:*" for w in words)]
        else:
            span_match = "rowid IN (SELECT rowid FROM spans_fts WHERE spans_fts MATCH ?)"
            span_params = [" ".join(f'"{w}"*' for w in words)]
        if table == "spans":
            return span_match, span_params
        return (
            f"trace_id IN (SELECT trace_id FROM spans WHERE project_id = ? AND {span_match})",
            [self.project_id, *span_params],
        )

    def _span_from_row(self, row: Sequence[Any]) -> Span:
        data = dict(zip(SPAN_COLUMNS, row, strict=True))
        for c in _SPAN_JSON:
            data[c] = self.dialect.load_json(data[c])
        return Span(
            name=data["name"],
            trace_id=data["trace_id"],
            span_id=data["span_id"],
            parent_id=data["parent_id"],
            start=data["start_time"],
            end=data["end_time"],
            kind=data["kind"],
            status=data["status"],
            level=data["level"],
            model=data["model"],
            model_params=data["model_params"] or {},
            input=data["input"],
            output=data["output"],
            usage=data["usage"] or {},
            cost_usd=data["cost_usd"],
            error=data["error"],
            session_id=data["session_id"],
            user_id=data["user_id"],
            release=data["release"],
            tags=data["tags"] or [],
            prompt_version=data["prompt_version"],
            ttft_ms=data["ttft_ms"],
            attributes=data["attributes"] or {},
        )

    def query_spans(
        self,
        where: dict[str, Any] | None = None,
        limit: int = 1000,
        offset: int = 0,
        order: str = "desc",
    ) -> list[Span]:
        """Filter spans. Keys: trace_id, name, kind, model, status, session_id, user_id, release,
        start_after, start_before, min_cost, min_duration_ms, error (bool), tag, root (bool),
        search (text), attributes (dict of equalities)."""
        clause, params = self._where(where, _SPAN_FILTERS, "spans")
        direction = "ASC" if order == "asc" else "DESC"
        rows = self.execute(
            f"SELECT {', '.join(SPAN_COLUMNS)} FROM spans WHERE {clause} "
            f"ORDER BY start_time {direction} LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        )
        return [self._span_from_row(r) for r in rows]

    def count_spans(self, where: dict[str, Any] | None = None) -> int:
        clause, params = self._where(where, _SPAN_FILTERS, "spans")
        return int(self.execute(f"SELECT COUNT(*) FROM spans WHERE {clause}", params)[0][0])

    def get_spans(self, trace_id: str) -> list[Span]:
        return self.query_spans({"trace_id": trace_id}, limit=100_000, order="asc")

    def _trace_from_row(self, row: Sequence[Any]) -> Trace:
        d = dict(zip(self._TRACE_COLS, row, strict=True))
        return Trace(
            trace_id=d["trace_id"],
            name=d["name"],
            session_id=d["session_id"],
            user_id=d["user_id"],
            release=d["release"],
            tags=self.dialect.load_json(d["tags"]) or [],
            input=self.dialect.load_json(d["input"]),
            output=self.dialect.load_json(d["output"]),
            start=d["start_time"],
            end=d["end_time"],
            duration_ms=d["duration_ms"],
            n_spans=d["n_spans"],
            input_tokens=d["input_tokens"],
            output_tokens=d["output_tokens"],
            cost_usd=d["cost_usd"],
            has_error=bool(d["has_error"]),
        )

    def query_traces(
        self, where: dict[str, Any] | None = None, limit: int = 100, offset: int = 0
    ) -> list[Trace]:
        clause, params = self._where(where, _TRACE_FILTERS, "traces")
        rows = self.execute(
            f"SELECT {', '.join(self._TRACE_COLS)} FROM traces WHERE {clause} "
            "ORDER BY start_time DESC LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        )
        return [self._trace_from_row(r) for r in rows]

    def count_traces(self, where: dict[str, Any] | None = None) -> int:
        clause, params = self._where(where, _TRACE_FILTERS, "traces")
        return int(self.execute(f"SELECT COUNT(*) FROM traces WHERE {clause}", params)[0][0])

    def get_trace(self, trace_id: str) -> Trace | None:
        found = self.query_traces({"trace_id": trace_id}, limit=1)
        return found[0] if found else None

    def delete_before(self, cutoff: float) -> int:
        """Retention: delete spans, traces and trace scores that started before ``cutoff``."""
        with self.tx() as cur:
            q = self.dialect.q
            cur.execute(
                q(
                    "DELETE FROM scores WHERE project_id = ? AND experiment_id IS NULL AND "
                    "trace_id IN (SELECT trace_id FROM traces WHERE project_id = ? AND "
                    "start_time < ?)"
                ),
                (self.project_id, self.project_id, cutoff),
            )
            cur.execute(
                q("DELETE FROM spans WHERE project_id = ? AND start_time < ?"),
                (self.project_id, cutoff),
            )
            n = cur.rowcount
            cur.execute(
                q("DELETE FROM traces WHERE project_id = ? AND start_time < ?"),
                (self.project_id, cutoff),
            )
        return int(n or 0)
