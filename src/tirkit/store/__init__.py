"""Storage for spans, traces, datasets, experiments and scores (schema: SPEC section 4).

One SQL layer, two dialects: SQLite (local, standard library only) and Postgres (server, via
psycopg). Every row carries ``project_id``; a store object is bound to one project and every query
filters on it. ``store.for_project(pid)`` returns a view on another project sharing the connection.

Layout: ``base`` (connection, transactions, schema), one mixin per domain (``spans``, ``datasets``,
``experiments``, ``scores``, ``analytics``, ``media``, ``config``), ``migrations`` and ``backends``.
This package imports only core modules (``tirkit.types``, ``tirkit.serialize``).
"""

from __future__ import annotations

from tirkit.store.backends import PostgresStore, SQLiteStore, SQLStore, open_store
from tirkit.store.base import DEFAULT_PROJECT, StoreBase
from tirkit.store.dialect import Dialect, PostgresDialect
from tirkit.store.migrations import MIGRATIONS
from tirkit.store.spans import SPAN_COLUMNS
from tirkit.types import Trace

__all__ = [
    "DEFAULT_PROJECT",
    "MIGRATIONS",
    "SPAN_COLUMNS",
    "Dialect",
    "PostgresDialect",
    "PostgresStore",
    "SQLStore",
    "SQLiteStore",
    "StoreBase",
    "Trace",
    "open_store",
]
