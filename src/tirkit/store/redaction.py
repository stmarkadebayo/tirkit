"""Per-project count of values redacted at ingestion (a trust signal shown in the UI)."""

from __future__ import annotations

import time

from tirkit.store.base import StoreBase


class RedactionMixin(StoreBase):
    def add_redactions(self, n: int) -> None:
        """Add ``n`` to this project's count (one upsert; callers skip it when ``n`` is 0)."""
        if n <= 0:
            return
        self.execute(
            "INSERT INTO redaction_stats (project_id, n, updated) VALUES (?, ?, ?) "
            "ON CONFLICT (project_id) DO UPDATE SET n = redaction_stats.n + excluded.n, "
            "updated = excluded.updated",
            [self.project_id, int(n), time.time()],
        )

    def redaction_count(self) -> int:
        rows = self.execute("SELECT n FROM redaction_stats WHERE project_id = ?", [self.project_id])
        return int(rows[0][0]) if rows else 0
