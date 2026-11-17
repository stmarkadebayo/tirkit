"""The concrete stores: SQLite (local) and Postgres (server), and ``open_store``."""

from __future__ import annotations

import os
import sqlite3
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tirkit.store.analytics import AnalyticsMixin
from tirkit.store.base import DEFAULT_PROJECT, _NoLock
from tirkit.store.config import ConfigMixin
from tirkit.store.datasets import DatasetsMixin
from tirkit.store.dialect import Dialect, PostgresDialect
from tirkit.store.experiments import ExperimentsMixin
from tirkit.store.media import MediaMixin
from tirkit.store.scores import ScoresMixin
from tirkit.store.spans import SpansMixin


class SQLStore(
    SpansMixin,
    DatasetsMixin,
    ExperimentsMixin,
    ScoresMixin,
    AnalyticsMixin,
    MediaMixin,
    ConfigMixin,
):
    """Dialect-neutral store. Subclasses provide a DB-API connection."""


class SQLiteStore(SQLStore):
    """Local store in one SQLite file (WAL mode, safe to share across threads).

    Fork-safe: a forked child opens its own connection and lock (SQLite connections must not be
    used across ``fork``, and the parent's lock may have been held by another thread)."""

    dialect = Dialect()

    def __init__(self, path: str | Path = "tirkit.db", project_id: str = DEFAULT_PROJECT) -> None:
        self.path = Path(path)
        self._path_arg = str(path)
        if self._path_arg != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(self._connect(), project_id)
        _sqlite_stores.add(self)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path_arg, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _after_fork_in_child(self) -> None:
        if self._path_arg == ":memory:":
            return  # a private in-memory database: the child's copy is its own
        # Keep the inherited connection referenced but unused: closing it in the child could
        # checkpoint or delete the parent's WAL file.
        _inherited_connections.append(self.conn)
        self.conn = self._connect()
        self._lock = threading.RLock()


_sqlite_stores: weakref.WeakSet[SQLiteStore] = weakref.WeakSet()
_inherited_connections: list[sqlite3.Connection] = []


def _reopen_sqlite_after_fork() -> None:
    for st in list(_sqlite_stores):
        st._after_fork_in_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reopen_sqlite_after_fork)


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


def open_store(url: str, project_id: str = DEFAULT_PROJECT) -> SQLStore:
    """``postgresql://...`` -> :class:`PostgresStore`; anything else is a SQLite path."""
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresStore(url, project_id)
    if url.startswith("sqlite:///"):
        url = url[len("sqlite:///") :]
    return SQLiteStore(url, project_id)
