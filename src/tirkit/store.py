"""Storage for spans, traces, datasets, experiments and scores (schema: SPEC section 4).

One SQL layer, two dialects: SQLite (local, standard library only) and Postgres (server, via
psycopg). Every row carries ``project_id``; a store object is bound to one project and every query
filters on it. ``store.for_project(pid)`` returns a view on another project sharing the connection.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tirkit.serialize import dumps
from tirkit.tracing import Span

DEFAULT_PROJECT = "default"
_WORD = re.compile(r"\w+", re.UNICODE)


class Dialect:
    name = "sqlite"
    param = "?"
    json = "TEXT"
    real = "REAL"
    bigint = "INTEGER"
    autoinc_pk = "INTEGER PRIMARY KEY AUTOINCREMENT"

    def q(self, sql: str) -> str:
        return sql if self.param == "?" else sql.replace("?", self.param)

    def json_get(self, col: str, key: str) -> str:
        safe = key.replace("'", "")
        return f"json_extract({col}, '$.\"{safe}\"')"

    def json_text(self, col: str) -> str:
        return col

    def tag_contains(self, col: str) -> str:
        return f"EXISTS (SELECT 1 FROM json_each({col}) WHERE json_each.value = ?)"

    def like(self) -> str:
        return "LIKE"

    def load_json(self, value: Any) -> Any:
        if value is None:
            return None
        return json.loads(value) if isinstance(value, str | bytes) else value


class PostgresDialect(Dialect):
    name = "postgres"
    param = "%s"
    json = "JSONB"
    real = "DOUBLE PRECISION"
    bigint = "BIGINT"
    autoinc_pk = "BIGSERIAL PRIMARY KEY"

    def json_get(self, col: str, key: str) -> str:
        safe = key.replace("'", "")
        return f"({col} ->> '{safe}')"

    def json_text(self, col: str) -> str:
        return f"({col})::text"

    def tag_contains(self, col: str) -> str:
        return f"jsonb_exists({col}, ?)"  # the `?` operator would clash with placeholders

    def like(self) -> str:
        return "ILIKE"

    def load_json(self, value: Any) -> Any:
        return value  # psycopg already parses JSONB


_FTS_BODY_NEW = (
    "coalesce(new.name, '') || ' ' || substr(coalesce(new.input, ''), 1, 200000) || ' ' || "
    "substr(coalesce(new.output, ''), 1, 200000)"
)

# Each migration is a list of statements, or a dict of lists per dialect.
# {JSON}, {REAL}, {BIGINT} and {AUTOPK} are filled per dialect.
MIGRATIONS: list[list[str] | dict[str, list[str]]] = [
    [
        """CREATE TABLE IF NOT EXISTS spans (
            project_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            parent_id TEXT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'generic',
            status TEXT NOT NULL DEFAULT 'ok',
            level TEXT NOT NULL DEFAULT 'default',
            start_time {REAL} NOT NULL,
            end_time {REAL},
            duration_ms {REAL},
            ttft_ms {REAL},
            model TEXT,
            model_params {JSON},
            input {JSON},
            output {JSON},
            usage {JSON},
            input_tokens {BIGINT},
            output_tokens {BIGINT},
            cost_usd {REAL},
            error TEXT,
            session_id TEXT,
            user_id TEXT,
            release TEXT,
            tags {JSON},
            prompt_version TEXT,
            attributes {JSON},
            PRIMARY KEY (project_id, span_id)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_spans_start ON spans (project_id, start_time)",
        "CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans (project_id, trace_id)",
        "CREATE INDEX IF NOT EXISTS ix_spans_name ON spans (project_id, name)",
        "CREATE INDEX IF NOT EXISTS ix_spans_model ON spans (project_id, model)",
        """CREATE TABLE IF NOT EXISTS traces (
            project_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            name TEXT,
            session_id TEXT,
            user_id TEXT,
            release TEXT,
            tags {JSON},
            input {JSON},
            output {JSON},
            start_time {REAL},
            end_time {REAL},
            duration_ms {REAL},
            n_spans INTEGER NOT NULL DEFAULT 0,
            input_tokens {BIGINT},
            output_tokens {BIGINT},
            cost_usd {REAL},
            has_error INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, trace_id)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_traces_start ON traces (project_id, start_time)",
        "CREATE INDEX IF NOT EXISTS ix_traces_session ON traces (project_id, session_id)",
        "CREATE INDEX IF NOT EXISTS ix_traces_user ON traces (project_id, user_id)",
    ],
    [
        """CREATE TABLE IF NOT EXISTS datasets (
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            created {REAL} NOT NULL,
            PRIMARY KEY (project_id, name)
        )""",
        """CREATE TABLE IF NOT EXISTS dataset_versions (
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            created {REAL} NOT NULL,
            n_items INTEGER NOT NULL,
            note TEXT,
            PRIMARY KEY (project_id, name, version)
        )""",
        """CREATE TABLE IF NOT EXISTS dataset_items (
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            idx INTEGER NOT NULL,
            input {JSON},
            expected {JSON},
            metadata {JSON},
            PRIMARY KEY (project_id, name, version, idx)
        )""",
        """CREATE TABLE IF NOT EXISTS experiments (
            project_id TEXT NOT NULL,
            experiment_id TEXT NOT NULL,
            name TEXT,
            dataset TEXT NOT NULL,
            version TEXT NOT NULL,
            target TEXT,
            prompt_version TEXT,
            config {JSON},
            status TEXT NOT NULL DEFAULT 'running',
            created {REAL} NOT NULL,
            finished {REAL},
            PRIMARY KEY (project_id, experiment_id)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_experiments_ds "
        "ON experiments (project_id, dataset, created)",
        """CREATE TABLE IF NOT EXISTS experiment_items (
            project_id TEXT NOT NULL,
            experiment_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            trace_id TEXT,
            output {JSON},
            latency_ms {REAL},
            cost_usd {REAL},
            error TEXT,
            PRIMARY KEY (project_id, experiment_id, idx)
        )""",
        """CREATE TABLE IF NOT EXISTS scores (
            project_id TEXT NOT NULL,
            score_id TEXT NOT NULL,
            trace_id TEXT,
            span_id TEXT,
            experiment_id TEXT,
            item_idx INTEGER,
            name TEXT NOT NULL,
            value {REAL},
            value_str TEXT,
            source TEXT NOT NULL,
            scorer_version TEXT,
            reason TEXT,
            error TEXT,
            author TEXT,
            created {REAL} NOT NULL,
            metadata {JSON},
            PRIMARY KEY (project_id, score_id)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_scores_trace ON scores (project_id, trace_id)",
        "CREATE INDEX IF NOT EXISTS ix_scores_exp ON scores (project_id, experiment_id, name)",
        "CREATE INDEX IF NOT EXISTS ix_scores_name ON scores (project_id, name, created)",
        """CREATE TABLE IF NOT EXISTS judge_cache (
            cache_key TEXT PRIMARY KEY,
            result {JSON} NOT NULL,
            created {REAL} NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS rubrics (
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            text TEXT NOT NULL,
            created {REAL} NOT NULL,
            PRIMARY KEY (project_id, name, version)
        )""",
    ],
    [
        """CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            name TEXT,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created {REAL} NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            settings {JSON},
            created {REAL} NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS memberships (
            user_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            role TEXT NOT NULL,
            PRIMARY KEY (user_id, project_id)
        )""",
        """CREATE TABLE IF NOT EXISTS api_keys (
            public_key TEXT PRIMARY KEY,
            secret_hash TEXT NOT NULL,
            project_id TEXT NOT NULL,
            name TEXT,
            created {REAL} NOT NULL,
            last_used {REAL}
        )""",
        """CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            csrf TEXT NOT NULL,
            created {REAL} NOT NULL,
            expires {REAL} NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS media (
            project_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            mime TEXT,
            size {BIGINT},
            created {REAL} NOT NULL,
            PRIMARY KEY (project_id, sha256)
        )""",
        """CREATE TABLE IF NOT EXISTS model_prices (
            project_id TEXT NOT NULL,
            model TEXT NOT NULL,
            prices {JSON} NOT NULL,
            PRIMARY KEY (project_id, model)
        )""",
    ],
    [
        """CREATE TABLE IF NOT EXISTS score_configs (
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            data_type TEXT NOT NULL,
            min_value {REAL},
            max_value {REAL},
            categories {JSON},
            description TEXT,
            created {REAL} NOT NULL,
            PRIMARY KEY (project_id, name)
        )""",
        """CREATE TABLE IF NOT EXISTS annotation_queues (
            project_id TEXT NOT NULL,
            queue_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            score_names {JSON} NOT NULL,
            created {REAL} NOT NULL,
            PRIMARY KEY (project_id, queue_id)
        )""",
        """CREATE TABLE IF NOT EXISTS queue_items (
            project_id TEXT NOT NULL,
            queue_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            locked_by TEXT,
            locked_until {REAL},
            completed_by TEXT,
            completed_at {REAL},
            created {REAL} NOT NULL,
            PRIMARY KEY (project_id, queue_id, item_id)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_queue_items_status "
        "ON queue_items (project_id, queue_id, status, created)",
    ],
    [
        """CREATE TABLE IF NOT EXISTS prompts (
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            created {REAL} NOT NULL,
            PRIMARY KEY (project_id, name)
        )""",
        """CREATE TABLE IF NOT EXISTS prompt_versions (
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            version INTEGER NOT NULL,
            kind TEXT NOT NULL,
            template {JSON} NOT NULL,
            config {JSON},
            message TEXT,
            created_by TEXT,
            created {REAL} NOT NULL,
            PRIMARY KEY (project_id, name, version)
        )""",
        """CREATE TABLE IF NOT EXISTS prompt_labels (
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            label TEXT NOT NULL,
            version INTEGER NOT NULL,
            updated {REAL} NOT NULL,
            PRIMARY KEY (project_id, name, label)
        )""",
        """CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload {JSON},
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            run_after {REAL} NOT NULL,
            locked_by TEXT,
            locked_until {REAL},
            result {JSON},
            error TEXT,
            created {REAL} NOT NULL,
            finished {REAL}
        )""",
        "CREATE INDEX IF NOT EXISTS ix_jobs_ready ON jobs (status, run_after)",
    ],
    [
        """CREATE TABLE IF NOT EXISTS evaluators (
            project_id TEXT NOT NULL,
            evaluator_id TEXT NOT NULL,
            name TEXT NOT NULL,
            filter {JSON},
            sampling_rate {REAL} NOT NULL,
            scorer {JSON} NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            cursor {REAL} NOT NULL,
            n_scored INTEGER NOT NULL DEFAULT 0,
            n_failed INTEGER NOT NULL DEFAULT 0,
            n_skipped_budget INTEGER NOT NULL DEFAULT 0,
            n_not_sampled INTEGER NOT NULL DEFAULT 0,
            created {REAL} NOT NULL,
            PRIMARY KEY (project_id, evaluator_id)
        )""",
        """CREATE TABLE IF NOT EXISTS judge_spend (
            project_id TEXT NOT NULL,
            day TEXT NOT NULL,
            usd {REAL} NOT NULL,
            PRIMARY KEY (project_id, day)
        )""",
        """CREATE TABLE IF NOT EXISTS alerts (
            project_id TEXT NOT NULL,
            alert_id TEXT NOT NULL,
            name TEXT NOT NULL,
            rule {JSON} NOT NULL,
            channel {JSON} NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'ok',
            last_value {REAL},
            last_checked {REAL},
            last_fired {REAL},
            created {REAL} NOT NULL,
            PRIMARY KEY (project_id, alert_id)
        )""",
        """CREATE TABLE IF NOT EXISTS alert_events (
            project_id TEXT NOT NULL,
            alert_id TEXT NOT NULL,
            at {REAL} NOT NULL,
            state TEXT NOT NULL,
            value {REAL},
            delivered INTEGER NOT NULL,
            error TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS judge_reports (
            project_id TEXT NOT NULL,
            report_id TEXT NOT NULL,
            rubric_name TEXT NOT NULL,
            rubric_version TEXT NOT NULL,
            model TEXT,
            source TEXT,
            summary {JSON} NOT NULL,
            created {REAL} NOT NULL,
            PRIMARY KEY (project_id, report_id)
        )""",
    ],
    [
        """CREATE TABLE IF NOT EXISTS leases (
            name TEXT PRIMARY KEY,
            holder TEXT NOT NULL,
            until {REAL} NOT NULL
        )""",
    ],
    # Indexed full-text search over span name, input and output (per dialect).
    {
        "sqlite": [
            "CREATE VIRTUAL TABLE IF NOT EXISTS spans_fts USING fts5("
            "body, tokenize = 'unicode61 remove_diacritics 2')",
            "CREATE TRIGGER IF NOT EXISTS spans_fts_ai AFTER INSERT ON spans BEGIN "
            "INSERT INTO spans_fts (rowid, body) VALUES (new.rowid, " + _FTS_BODY_NEW + "); END",
            "CREATE TRIGGER IF NOT EXISTS spans_fts_au AFTER UPDATE ON spans BEGIN "
            "DELETE FROM spans_fts WHERE rowid = old.rowid; "
            "INSERT INTO spans_fts (rowid, body) VALUES (new.rowid, " + _FTS_BODY_NEW + "); END",
            "CREATE TRIGGER IF NOT EXISTS spans_fts_ad AFTER DELETE ON spans BEGIN "
            "DELETE FROM spans_fts WHERE rowid = old.rowid; END",
            "INSERT INTO spans_fts (rowid, body) SELECT rowid, "
            + _FTS_BODY_NEW.replace("new.", "")
            + " FROM spans",
        ],
        "postgres": [
            "ALTER TABLE spans ADD COLUMN IF NOT EXISTS search_tsv tsvector GENERATED ALWAYS AS "
            "(to_tsvector('simple', left(coalesce(name, '') || ' ' || coalesce(input::text, '') "
            "|| ' ' || coalesce(output::text, ''), 200000))) STORED",
            "CREATE INDEX IF NOT EXISTS ix_spans_search ON spans USING GIN (search_tsv)",
        ],
    },
]

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


@dataclass
class Trace:
    """Summary of one trace (aggregated over its spans)."""

    trace_id: str
    name: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    release: str | None = None
    tags: list[str] = field(default_factory=list)
    input: Any = None
    output: Any = None
    start: float | None = None
    end: float | None = None
    duration_ms: float | None = None
    n_spans: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    has_error: bool = False


def _pct(sorted_values: list[float], q: float) -> float:
    pos = q * (len(sorted_values) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _num_row(row: dict[str, Any]) -> dict[str, Any]:
    """Postgres returns Decimal for SUM(bigint); make rows JSON-friendly."""
    from decimal import Decimal

    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}


class SQLStore:
    """Dialect-neutral store. Subclasses provide a DB-API connection."""

    dialect: Dialect = Dialect()

    def __init__(self, conn: Any, project_id: str = DEFAULT_PROJECT, lock: Any = None) -> None:
        self.conn = conn
        self.project_id = project_id
        self._lock = lock if lock is not None else threading.RLock()

    # -- plumbing ---------------------------------------------------------------------------

    def for_project(self, project_id: str) -> SQLStore:
        view = object.__new__(type(self))
        view.__dict__.update(self.__dict__)
        view.project_id = project_id
        return view

    @contextmanager
    def tx(self) -> Iterator[Any]:
        """A cursor inside one transaction."""
        with self._lock:
            cur = self.conn.cursor()
            try:
                yield cur
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            finally:
                cur.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self.tx() as cur:
            cur.execute(self.dialect.q(sql), tuple(params))
            return list(cur.fetchall()) if cur.description else []

    def _fmt(self, stmt: str) -> str:
        d = self.dialect
        return stmt.format(JSON=d.json, REAL=d.real, BIGINT=d.bigint, AUTOPK=d.autoinc_pk)

    def init_schema(self) -> None:
        """Create or upgrade tables (idempotent)."""
        with self.tx() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
            )
            cur.execute("SELECT version FROM schema_migrations")
            done = {r[0] for r in cur.fetchall()}
            for version, entry in enumerate(MIGRATIONS, start=1):
                if version in done:
                    continue
                statements = entry[self.dialect.name] if isinstance(entry, dict) else entry
                for stmt in statements:
                    cur.execute(self._fmt(stmt))
                cur.execute(
                    self.dialect.q("INSERT INTO schema_migrations (version) VALUES (?)"),
                    (version,),
                )

    def _json(self, value: Any) -> str | None:
        return None if value is None else dumps(value)

    # -- spans ------------------------------------------------------------------------------

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

    _TRACE_SUMMARY_COLS = (
        "project_id, trace_id, name, session_id, user_id, release, tags, input, output, "
        "start_time, end_time, duration_ms, n_spans, input_tokens, output_tokens, cost_usd, "
        "has_error"
    )

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

    # -- traces -----------------------------------------------------------------------------

    _TRACE_COLS = (
        "trace_id name session_id user_id release tags input output start_time end_time "
        "duration_ms n_spans input_tokens output_tokens cost_usd has_error"
    ).split()

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

    # -- datasets -------------------------------------------------------------------------------

    def save_dataset(self, ds: Any, description: str | None = None, note: str | None = None) -> str:
        """Store a dataset version (idempotent: the same content keeps the same version)."""
        version = ds.version
        now = time.time()
        q = self.dialect.q
        with self.tx() as cur:
            cur.execute(
                q(
                    "INSERT INTO datasets (project_id, name, description, created) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (project_id, name) DO UPDATE SET "
                    "description = COALESCE(excluded.description, datasets.description)"
                ),
                (self.project_id, ds.name, description, now),
            )
            cur.execute(
                q(
                    "SELECT 1 FROM dataset_versions WHERE project_id = ? AND name = ? "
                    "AND version = ?"
                ),
                (self.project_id, ds.name, version),
            )
            if cur.fetchone():
                return str(version)
            cur.execute(
                q(
                    "INSERT INTO dataset_versions (project_id, name, version, created, n_items, "
                    "note) VALUES (?, ?, ?, ?, ?, ?)"
                ),
                (self.project_id, ds.name, version, now, len(ds.examples), note),
            )
            cur.executemany(
                q(
                    "INSERT INTO dataset_items (project_id, name, version, idx, input, expected, "
                    "metadata) VALUES (?, ?, ?, ?, ?, ?, ?)"
                ),
                [
                    (
                        self.project_id,
                        ds.name,
                        version,
                        i,
                        self._json(e.input),
                        self._json(e.expected),
                        self._json(e.metadata or None),
                    )
                    for i, e in enumerate(ds.examples)
                ],
            )
        return str(version)

    def load_dataset(self, name: str, version: str | None = None) -> Any:
        """Load a dataset version (latest when ``version`` is None)."""
        from tirkit.datasets import Dataset, Example

        if version is None:
            rows = self.execute(
                "SELECT version FROM dataset_versions WHERE project_id = ? AND name = ? "
                "ORDER BY created DESC LIMIT 1",
                [self.project_id, name],
            )
            if not rows:
                raise KeyError(f"no dataset named {name!r}")
            version = rows[0][0]
        items = self.execute(
            "SELECT input, expected, metadata FROM dataset_items WHERE project_id = ? AND name = ? "
            "AND version = ? ORDER BY idx",
            [self.project_id, name, version],
        )
        if not items:
            found = self.execute(
                "SELECT 1 FROM dataset_versions WHERE project_id = ? AND name = ? AND version = ?",
                [self.project_id, name, version],
            )
            if not found:
                raise KeyError(f"no version {version!r} of dataset {name!r}")
        load = self.dialect.load_json
        ds = Dataset(name, [Example(load(i) or {}, load(e), load(m) or {}) for i, e, m in items])
        if ds.version != version:
            raise ValueError(f"dataset {name}@{version} is corrupt: content hash {ds.version}")
        return ds

    def list_datasets(self) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT d.name, d.description, d.created, "
            "(SELECT COUNT(*) FROM dataset_versions v WHERE v.project_id = d.project_id "
            "AND v.name = d.name), "
            "(SELECT v.version FROM dataset_versions v WHERE v.project_id = d.project_id "
            "AND v.name = d.name ORDER BY v.created DESC LIMIT 1), "
            "(SELECT v.n_items FROM dataset_versions v WHERE v.project_id = d.project_id "
            "AND v.name = d.name ORDER BY v.created DESC LIMIT 1) "
            "FROM datasets d WHERE d.project_id = ? ORDER BY d.created DESC",
            [self.project_id],
        )
        return [
            {
                "name": r[0],
                "description": r[1],
                "created": r[2],
                "n_versions": r[3],
                "latest_version": r[4],
                "n_items": r[5],
            }
            for r in rows
        ]

    def dataset_versions(self, name: str) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT version, created, n_items, note FROM dataset_versions WHERE project_id = ? "
            "AND name = ? ORDER BY created DESC",
            [self.project_id, name],
        )
        return [{"version": r[0], "created": r[1], "n_items": r[2], "note": r[3]} for r in rows]

    # -- experiments and scores ---------------------------------------------------------------

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
        from tirkit.runner import RunResult

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

    # -- analytics -------------------------------------------------------------------------------

    def sessions(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT session_id, COUNT(*), MIN(start_time), MAX(end_time), SUM(cost_usd), "
            "SUM(input_tokens), SUM(output_tokens), MAX(has_error), MAX(user_id) FROM traces "
            "WHERE project_id = ? AND session_id IS NOT NULL GROUP BY session_id "
            "ORDER BY MAX(start_time) DESC LIMIT ? OFFSET ?",
            [self.project_id, limit, offset],
        )
        keys = "session_id n_traces start end cost_usd input_tokens output_tokens has_error user_id"
        return [_num_row(dict(zip(keys.split(), r, strict=True))) for r in rows]

    def users(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT user_id, COUNT(*), MIN(start_time), MAX(start_time), SUM(cost_usd), "
            "SUM(input_tokens), SUM(output_tokens), SUM(has_error) FROM traces "
            "WHERE project_id = ? AND user_id IS NOT NULL GROUP BY user_id "
            "ORDER BY MAX(start_time) DESC LIMIT ? OFFSET ?",
            [self.project_id, limit, offset],
        )
        keys = "user_id n_traces first_seen last_seen cost_usd input_tokens output_tokens n_errors"
        return [_num_row(dict(zip(keys.split(), r, strict=True))) for r in rows]

    def filter_values(self) -> dict[str, list[Any]]:
        """Distinct values for UI filter menus."""
        out: dict[str, list[Any]] = {}
        for key, table, col in (
            ("names", "traces", "name"),
            ("span_names", "spans", "name"),
            ("models", "spans", "model"),
            ("releases", "traces", "release"),
            ("kinds", "spans", "kind"),
        ):
            rows = self.execute(
                f"SELECT DISTINCT {col} FROM {table} WHERE project_id = ? AND {col} IS NOT NULL "
                f"ORDER BY {col} LIMIT 500",
                [self.project_id],
            )
            out[key] = [r[0] for r in rows]
        rows = self.execute(
            "SELECT DISTINCT name FROM scores WHERE project_id = ? ORDER BY name LIMIT 500",
            [self.project_id],
        )
        out["scores"] = [r[0] for r in rows]
        return out

    def metrics(
        self,
        since: float,
        until: float,
        *,
        buckets: int = 48,
        group_by: str | None = None,
        max_latency_samples: int = 100_000,
    ) -> dict[str, Any]:
        """Dashboard series over [since, until): per bucket and optional group (model, name,
        release, user_id, kind): spans, traces, errors, tokens, cost, latency p50/p95/p99, and
        score averages. Latency percentiles are exact up to ``max_latency_samples`` traces
        (SPEC §6.8); the sample size is reported."""
        width = max((until - since) / buckets, 1.0)
        group_col = {
            "model": "model",
            "name": "name",
            "release": "release",
            "user_id": "user_id",
            "kind": "kind",
        }.get(group_by or "")
        g = f"COALESCE({group_col}, '(none)')" if group_col else "'all'"
        bucket = f"CAST((start_time - {float(since)!r}) / {width!r} AS INTEGER)"
        rows = self.execute(
            f"SELECT {bucket} AS b, {g} AS g, COUNT(*), "
            "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), SUM(input_tokens), "
            "SUM(output_tokens), SUM(cost_usd) FROM spans "
            "WHERE project_id = ? AND start_time >= ? AND start_time < ? "
            "GROUP BY 1, 2",
            [self.project_id, since, until],
        )
        series: dict[str, dict[int, dict[str, Any]]] = {}
        for b, grp, n, errs, tin, tout, cost in rows:
            series.setdefault(str(grp), {})[int(b)] = {
                "spans": int(n),
                "errors": int(errs or 0),
                "input_tokens": int(tin or 0),
                "output_tokens": int(tout or 0),
                "cost_usd": float(cost or 0.0),
            }
        trace_group = group_col if group_col in {"name", "release", "user_id"} else None
        tg = f"COALESCE({trace_group}, '(none)')" if trace_group else "'all'"
        lat_rows = self.execute(
            f"SELECT {bucket}, {tg}, duration_ms, has_error FROM traces "
            "WHERE project_id = ? AND start_time >= ? AND start_time < ? "
            "AND duration_ms IS NOT NULL ORDER BY start_time DESC LIMIT ?",
            [self.project_id, since, until, max_latency_samples],
        )
        lat: dict[tuple[str, int], list[float]] = {}
        tcount: dict[tuple[str, int], list[int]] = {}
        for b, grp, dur, err in lat_rows:
            lat.setdefault((str(grp), int(b)), []).append(float(dur))
            c = tcount.setdefault((str(grp), int(b)), [0, 0])
            c[0] += 1
            c[1] += int(err or 0)
        for (grp, b), durs in lat.items():
            durs.sort()
            point = series.setdefault(grp, {}).setdefault(b, {"spans": 0, "errors": 0})
            point.update(
                traces=tcount[(grp, b)][0],
                trace_errors=tcount[(grp, b)][1],
                p50_ms=_pct(durs, 0.5),
                p95_ms=_pct(durs, 0.95),
                p99_ms=_pct(durs, 0.99),
            )
        score_rows = self.execute(
            f"SELECT CAST((created - {float(since)!r}) / {width!r} AS INTEGER), name, AVG(value), "
            "COUNT(value) FROM scores WHERE project_id = ? AND created >= ? AND created < ? "
            "AND value IS NOT NULL AND experiment_id IS NULL GROUP BY 1, name",
            [self.project_id, since, until],
        )
        scores: dict[str, dict[int, dict[str, float]]] = {}
        for b, name, avg, n in score_rows:
            scores.setdefault(str(name), {})[int(b)] = {"mean": float(avg), "n": int(n)}
        return {
            "since": since,
            "until": until,
            "bucket_s": width,
            "buckets": buckets,
            "group_by": group_col,
            "series": {
                grp: [{"bucket": b, "t": since + b * width, **v} for b, v in sorted(pts.items())]
                for grp, pts in series.items()
            },
            "scores": {
                name: [{"bucket": b, "t": since + b * width, **v} for b, v in sorted(pts.items())]
                for name, pts in scores.items()
            },
            "latency_sample_size": len(lat_rows),
            "latency_sample_capped": len(lat_rows) >= max_latency_samples,
        }

    # -- media and retention ------------------------------------------------------------------

    def record_media(self, sha256: str, mime: str | None, size: int) -> None:
        self.execute(
            "INSERT INTO media (project_id, sha256, mime, size, created) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (project_id, sha256) DO NOTHING",
            [self.project_id, sha256, mime, size, time.time()],
        )

    def media_info(self, sha256: str) -> dict[str, Any] | None:
        rows = self.execute(
            "SELECT mime, size FROM media WHERE project_id = ? AND sha256 = ?",
            [self.project_id, sha256],
        )
        return {"mime": rows[0][0], "size": rows[0][1]} if rows else None

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

    def get_prices(self) -> dict[str, dict[str, float]]:
        rows = self.execute(
            "SELECT model, prices FROM model_prices WHERE project_id = ?", [self.project_id]
        )
        return {r[0]: self.dialect.load_json(r[1]) for r in rows}

    def set_price(self, model: str, prices: dict[str, float] | None) -> None:
        if prices is None:
            self.execute(
                "DELETE FROM model_prices WHERE project_id = ? AND model = ?",
                [self.project_id, model],
            )
            return
        self.execute(
            "INSERT INTO model_prices (project_id, model, prices) VALUES (?, ?, ?) "
            "ON CONFLICT (project_id, model) DO UPDATE SET prices = excluded.prices",
            [self.project_id, model, self._json(prices)],
        )

    # -- judge cache and rubrics ---------------------------------------------------------------

    def cache_get(self, key: str) -> Any:
        rows = self.execute("SELECT result FROM judge_cache WHERE cache_key = ?", [key])
        return self.dialect.load_json(rows[0][0]) if rows else None

    def cache_put(self, key: str, result: Any) -> None:
        self.execute(
            "INSERT INTO judge_cache (cache_key, result, created) VALUES (?, ?, ?) "
            "ON CONFLICT (cache_key) DO UPDATE SET result = excluded.result",
            [key, self._json(result), time.time()],
        )

    def save_rubric(self, name: str, version: str, text: str) -> None:
        self.execute(
            "INSERT INTO rubrics (project_id, name, version, text, created) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (project_id, name, version) DO NOTHING",
            [self.project_id, name, version, text, time.time()],
        )

    def list_rubrics(self, name: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT name, version, text, created FROM rubrics WHERE project_id = ?"
        params: list[Any] = [self.project_id]
        if name:
            sql += " AND name = ?"
            params.append(name)
        rows = self.execute(sql + " ORDER BY created", params)
        return [{"name": r[0], "version": r[1], "text": r[2], "created": r[3]} for r in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class SQLiteStore(SQLStore):
    """Local store in one SQLite file (WAL mode, safe to share across threads)."""

    dialect = Dialect()

    def __init__(self, path: str | Path = "tirkit.db", project_id: str = DEFAULT_PROJECT) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        super().__init__(conn, project_id)


class PostgresStore(SQLStore):
    """Server store on Postgres (``pip install "tirkit[server]"``), with a connection pool so
    concurrent requests do not serialise on one connection."""

    dialect = PostgresDialect()

    def __init__(self, dsn: str, project_id: str = DEFAULT_PROJECT, pool: Any = None) -> None:
        from psycopg_pool import ConnectionPool

        self.dsn = dsn
        self.pool = pool or ConnectionPool(dsn, min_size=1, max_size=16, open=True)
        super().__init__(None, project_id, lock=_NoLock())

    @contextmanager
    def tx(self) -> Iterator[Any]:
        with self.pool.connection() as conn, conn.cursor() as cur:
            yield cur  # the pool commits on success and rolls back on error

    def close(self) -> None:
        self.pool.close()


class _NoLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


def open_store(url: str, project_id: str = DEFAULT_PROJECT) -> SQLStore:
    """``postgresql://...`` -> :class:`PostgresStore`; anything else is a SQLite path."""
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresStore(url, project_id)
    if url.startswith("sqlite:///"):
        url = url[len("sqlite:///") :]
    return SQLiteStore(url, project_id)
