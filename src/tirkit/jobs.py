"""Postgres/SQLite-backed job queue and worker (SPEC §7: no Redis).

Jobs are rows; a worker claims the oldest ready job atomically (``FOR UPDATE SKIP LOCKED`` on
Postgres), runs the handler for its kind, and records the result. Crashed or timed-out jobs are
re-queued up to ``MAX_ATTEMPTS``. Periodic work (retention, online evals, alerts) is scheduled by
the worker loop itself.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from typing import Any

from tirkit.store import SQLStore

log = logging.getLogger("tirkit.worker")

MAX_ATTEMPTS = 3
LEASE_S = 3600.0
HANDLERS: dict[str, Callable[[SQLStore, dict[str, Any]], Any]] = {}
PERIODIC: list[tuple[float, Callable[[SQLStore], None]]] = []


def handler(kind: str) -> Callable[[Callable[[SQLStore, dict[str, Any]], Any]], Any]:
    def register(fn: Callable[[SQLStore, dict[str, Any]], Any]) -> Any:
        HANDLERS[kind] = fn
        return fn

    return register


def periodic(every_s: float) -> Callable[[Callable[[SQLStore], None]], Any]:
    """Run ``fn(root_store)`` every ``every_s`` seconds in each worker."""

    def register(fn: Callable[[SQLStore], None]) -> Any:
        PERIODIC.append((every_s, fn))
        return fn

    return register


def enqueue(store: SQLStore, kind: str, payload: dict[str, Any], *, delay_s: float = 0) -> str:
    job_id = "j_" + uuid.uuid4().hex[:16]
    now = time.time()
    store.execute(
        "INSERT INTO jobs (job_id, project_id, kind, payload, status, attempts, run_after, "
        "created) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?)",
        [job_id, store.project_id, kind, store._json(payload), now + delay_s, now],
    )
    return job_id


def get_job(store: SQLStore, job_id: str) -> dict[str, Any] | None:
    rows = store.execute(
        "SELECT job_id, kind, status, attempts, result, error, created, finished FROM jobs "
        "WHERE job_id = ? AND project_id = ?",
        [job_id, store.project_id],
    )
    if not rows:
        return None
    r = rows[0]
    keys = "job_id kind status attempts result error created finished".split()
    out = dict(zip(keys, r, strict=True))
    out["result"] = store.dialect.load_json(out["result"])
    return out


def list_jobs(store: SQLStore, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = (
        "SELECT job_id, kind, status, attempts, error, created, finished FROM jobs "
        "WHERE project_id = ?"
    )
    params: list[Any] = [store.project_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    rows = store.execute(sql + " ORDER BY created DESC LIMIT ?", [*params, limit])
    keys = "job_id kind status attempts error created finished".split()
    return [dict(zip(keys, r, strict=True)) for r in rows]


def claim(store: SQLStore, worker_id: str) -> dict[str, Any] | None:
    now = time.time()
    lock = " FOR UPDATE SKIP LOCKED" if store.dialect.name == "postgres" else ""
    # Requeue expired leases first.
    store.execute(
        "UPDATE jobs SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END, "
        "error = CASE WHEN attempts >= ? THEN 'lease expired' ELSE error END, locked_by = NULL "
        "WHERE status = 'running' AND locked_until < ?",
        [MAX_ATTEMPTS, MAX_ATTEMPTS, now],
    )
    rows = store.execute(
        "UPDATE jobs SET status = 'running', locked_by = ?, locked_until = ?, "
        "attempts = attempts + 1 WHERE job_id = (SELECT job_id FROM jobs WHERE status = 'queued' "
        f"AND run_after <= ? ORDER BY run_after LIMIT 1{lock}) "
        "RETURNING job_id, project_id, kind, payload, attempts",
        [worker_id, now + LEASE_S, now],
    )
    if not rows:
        return None
    job_id, project_id, kind, payload, attempts = rows[0]
    return {
        "job_id": job_id,
        "project_id": project_id,
        "kind": kind,
        "payload": store.dialect.load_json(payload) or {},
        "attempts": attempts,
    }


def run_one(store: SQLStore, worker_id: str) -> bool:
    """Claim and run one job. Returns False when there was nothing to do."""
    from tirkit import worker_tasks  # noqa: F401  (registers handlers)

    job = claim(store, worker_id)
    if job is None:
        return False
    fn = HANDLERS.get(job["kind"])
    project_store = store.for_project(job["project_id"])
    try:
        if fn is None:
            raise RuntimeError(f"no handler for job kind {job['kind']!r}")
        result = fn(project_store, job["payload"])
        store.execute(
            "UPDATE jobs SET status = 'done', result = ?, finished = ?, locked_by = NULL "
            "WHERE job_id = ?",
            [store._json(result), time.time(), job["job_id"]],
        )
    except Exception as exc:  # noqa: BLE001 - recorded on the job
        permanent = isinstance(exc, (ValueError, KeyError)) or fn is None
        retry = job["attempts"] < MAX_ATTEMPTS and not permanent
        log.warning("job %s (%s) failed: %s", job["job_id"], job["kind"], exc)
        store.execute(
            "UPDATE jobs SET status = ?, error = ?, run_after = ?, finished = ?, locked_by = NULL "
            "WHERE job_id = ?",
            [
                "queued" if retry else "failed",
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"[:4000],
                time.time() + 30 * job["attempts"],
                None if retry else time.time(),
                job["job_id"],
            ],
        )
    return True


def acquire_lease(store: SQLStore, name: str, holder: str, ttl_s: float) -> bool:
    """Take or renew a named lease so only one worker runs a periodic task at a time."""
    now = time.time()
    store.execute(
        "INSERT INTO leases (name, holder, until) VALUES (?, ?, ?) ON CONFLICT (name) DO NOTHING",
        [name, holder, now + ttl_s],
    )
    rows = store.execute(
        "UPDATE leases SET holder = ?, until = ? WHERE name = ? AND (holder = ? OR until < ?) "
        "RETURNING holder",
        [holder, now + ttl_s, name, holder, now],
    )
    return bool(rows)


class Worker:
    """Polls for jobs and runs periodic tasks until stopped."""

    def __init__(self, store: SQLStore, *, poll_s: float = 1.0) -> None:
        from tirkit import worker_tasks  # noqa: F401  (registers handlers)

        self.store = store
        self.poll_s = poll_s
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()
        self._last: dict[int, float] = {}

    def tick(self) -> None:
        now = time.time()
        for i, (every, fn) in enumerate(PERIODIC):
            if now - self._last.get(i, 0) >= every:
                self._last[i] = now
                lease = f"periodic:{fn.__module__}.{fn.__name__}"
                if not acquire_lease(self.store, lease, self.worker_id, max(every * 3, 120)):
                    continue  # another worker holds it
                try:
                    fn(self.store)
                except Exception:  # noqa: BLE001
                    log.warning("periodic task %s failed", fn.__name__, exc_info=True)
        while not self._stop.is_set() and run_one(self.store, self.worker_id):
            pass

    def run_forever(self) -> None:
        log.info("tirkit worker %s started", self.worker_id)
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.poll_s)

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="tirkit-worker", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
