"""Several processes (server replicas and workers in Kubernetes) may create the schema at the same
moment on a fresh Postgres database; none of them may fail."""

from __future__ import annotations

import os
import threading

import pytest

URL = os.environ.get("TIRKIT_TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="TIRKIT_TEST_POSTGRES_URL not set"),
]


def test_concurrent_init_schema_on_a_fresh_database() -> None:
    psycopg = pytest.importorskip("psycopg")
    from tirkit.store import MIGRATIONS, PostgresStore

    for _ in range(3):
        with psycopg.connect(URL, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        stores = [PostgresStore(URL) for _ in range(6)]
        errors: list[BaseException] = []
        barrier = threading.Barrier(len(stores))

        def init(
            st: PostgresStore,
            barrier: threading.Barrier = barrier,
            errors: list[BaseException] = errors,
        ) -> None:
            barrier.wait()
            try:
                st.init_schema()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=init, args=(st,)) for st in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = stores[0].execute("SELECT COUNT(*) FROM schema_migrations")
        for st in stores:
            st.close()
        assert errors == []
        assert rows[0][0] == len(MIGRATIONS)
