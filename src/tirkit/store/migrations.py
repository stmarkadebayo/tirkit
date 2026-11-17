"""Schema migrations, applied in order by ``init_schema``."""

from __future__ import annotations

_FTS_BODY_NEW = (
    "coalesce(new.name, '') || ' ' || substr(coalesce(new.input, ''), 1, 200000) || ' ' || "
    "substr(coalesce(new.output, ''), 1, 200000)"
)


# Phase B tables, shared by both dialects (the append-only triggers differ per dialect).
_SECURITY = [
    """CREATE TABLE IF NOT EXISTS rate_limits (
        key TEXT PRIMARY KEY,
        a {REAL} NOT NULL,
        b {REAL} NOT NULL,
        ts {REAL} NOT NULL,
        ok INTEGER NOT NULL,
        updated {REAL} NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id {AUTOPK},
        at {REAL} NOT NULL,
        actor TEXT,
        actor_id TEXT,
        ip TEXT,
        action TEXT NOT NULL,
        project_id TEXT,
        target TEXT,
        detail {JSON}
    )""",
    "CREATE INDEX IF NOT EXISTS ix_audit_log_at ON audit_log (at)",
    "CREATE INDEX IF NOT EXISTS ix_audit_log_project ON audit_log (project_id, at)",
    """CREATE TABLE IF NOT EXISTS redaction_stats (
        project_id TEXT PRIMARY KEY,
        n {BIGINT} NOT NULL,
        updated {REAL} NOT NULL
    )""",
    "ALTER TABLE api_keys ADD COLUMN expires_at {REAL}",
    "ALTER TABLE api_keys ADD COLUMN rotated_to TEXT",
]


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
    # Security (phase B): shared rate limits, append-only audit log, redaction counts, key expiry.
    {
        "sqlite": _SECURITY
        + [
            "CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log "
            "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log "
            "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
        ],
        "postgres": _SECURITY
        + [
            "CREATE OR REPLACE FUNCTION tirkit_audit_append_only() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'audit_log is append-only'; END; $$ LANGUAGE plpgsql",
            "CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log "
            "FOR EACH ROW EXECUTE FUNCTION tirkit_audit_append_only()",
        ],
    },
]
