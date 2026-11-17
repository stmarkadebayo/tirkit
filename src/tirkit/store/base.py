"""Store plumbing shared by every domain mixin: connection, transactions, schema."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Self

from tirkit.serialize import dumps
from tirkit.store.dialect import Dialect

DEFAULT_PROJECT = "default"
# Postgres advisory lock key held while migrations run ("tirk" as a 32-bit integer).
SCHEMA_LOCK_ID = 0x7469726B


class StoreBase:
    """Connection, transactions and schema. Each mixin adds one domain's queries."""

    dialect: Dialect = Dialect()

    def __init__(self, conn: Any, project_id: str = DEFAULT_PROJECT, lock: Any = None) -> None:
        self.conn = conn
        self.project_id = project_id
        self._lock = lock if lock is not None else threading.RLock()

    def for_project(self, project_id: str) -> Self:
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
            if self.dialect.name == "postgres":
                # Replicas and workers starting together on a fresh database would otherwise race
                # on CREATE TABLE IF NOT EXISTS. Released when the transaction ends.
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
            )
            cur.execute("SELECT version FROM schema_migrations")
            done = {r[0] for r in cur.fetchall()}
            # Read through the package so tests can swap ``tirkit.store.MIGRATIONS``.
            from tirkit import store as package

            for version, entry in enumerate(package.MIGRATIONS, start=1):
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

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class _NoLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None
