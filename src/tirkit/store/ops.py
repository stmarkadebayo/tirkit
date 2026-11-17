"""Cheap whole-installation figures for monitoring: job queue and judge spend.

Not bound to one project: these read across every project, and the results carry no project or
trace identifiers (they feed Prometheus gauges, where per-project labels would grow without bound).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any


def job_queue(store: Any, now: float | None = None) -> dict[str, float]:
    """Queued and running job counts, and the age of the oldest job that is ready to run.

    Uses the ``(status, run_after)`` index; finished jobs are not scanned.
    """
    now = time.time() if now is None else now
    counts = dict(
        store.execute(
            "SELECT status, COUNT(*) FROM jobs WHERE status IN ('queued', 'running') "
            "GROUP BY status"
        )
    )
    oldest = store.execute(
        "SELECT MIN(run_after) FROM jobs WHERE status = 'queued' AND run_after <= ?", [now]
    )[0][0]
    return {
        "queued": int(counts.get("queued", 0)),
        "running": int(counts.get("running", 0)),
        "oldest_ready_age_s": max(0.0, now - float(oldest)) if oldest is not None else 0.0,
    }


def judge_spend(store: Any, day: str | None = None) -> dict[str, float]:
    """LLM-judge spend in USD: today (UTC, the same day key the online evaluators use) and in
    total over the rows still stored."""
    day = day or datetime.now(UTC).strftime("%Y-%m-%d")
    row = store.execute(
        "SELECT COALESCE(SUM(CASE WHEN day = ? THEN usd ELSE 0 END), 0), COALESCE(SUM(usd), 0) "
        "FROM judge_spend",
        [day],
    )[0]
    return {"today_usd": float(row[0]), "total_usd": float(row[1])}
