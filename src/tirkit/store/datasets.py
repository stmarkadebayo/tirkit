"""Versioned datasets."""

from __future__ import annotations

import time
from typing import Any

from tirkit.store.base import StoreBase
from tirkit.types import Dataset, Example


class DatasetsMixin(StoreBase):
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
