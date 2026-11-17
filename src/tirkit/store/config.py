"""Model prices, the judge cache and rubric versions."""

from __future__ import annotations

import time
from typing import Any

from tirkit.store.base import StoreBase


class ConfigMixin(StoreBase):
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
