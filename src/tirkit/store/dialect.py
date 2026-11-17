"""SQL dialects: SQLite (default) and Postgres."""

from __future__ import annotations

import json
from typing import Any


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
