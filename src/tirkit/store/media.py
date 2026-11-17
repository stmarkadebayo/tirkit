"""Metadata for binary attachments held in the media store."""

from __future__ import annotations

import time
from typing import Any

from tirkit.store.base import StoreBase


class MediaMixin(StoreBase):
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
