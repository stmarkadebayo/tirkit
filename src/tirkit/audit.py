"""Append-only audit log: who did what, when and from where (sign-ins, keys, members, settings,
exports and deletions).

Rows are only ever inserted. Database triggers refuse ``UPDATE`` and ``DELETE`` on ``audit_log``
(SQLite and Postgres), so a bug or an attacker with the application's credentials cannot quietly
rewrite history through the app. Archiving old rows is an operator task: see
``docs/site/security.md``.
"""

from __future__ import annotations

import time
from typing import Any

from tirkit.store import SQLStore

# Every action the server records. Kept as a set so a typo fails a test, not silently.
ACTIONS = {
    "setup",
    "login.success",
    "login.failure",
    "logout",
    "project.create",
    "settings.update",
    "price.set",
    "member.add",
    "member.remove",
    "key.create",
    "key.rotate",
    "key.delete",
    "alert.create",
    "alert.delete",
    "evaluator.delete",
    "export.traces",
    "export.labels",
}

_COLUMNS = ("id", "at", "actor", "actor_id", "ip", "action", "project_id", "target", "detail")


def record(
    store: SQLStore,
    action: str,
    *,
    actor: str | None = None,
    actor_id: str | None = None,
    ip: str | None = None,
    project_id: str | None = None,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
    at: float | None = None,
) -> None:
    """Append one event. ``actor`` is an email, ``api:<public key>`` or None (anonymous)."""
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    store.execute(
        "INSERT INTO audit_log (at, actor, actor_id, ip, action, project_id, target, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            time.time() if at is None else at,
            actor,
            actor_id,
            ip,
            action,
            project_id,
            target,
            store._json(detail) if detail else None,
        ],
    )


def entries(
    store: SQLStore,
    *,
    project_id: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    before_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Newest first. Page with ``before_id`` (the last ``id`` of the previous page)."""
    where: list[str] = []
    params: list[Any] = []
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    if action:
        if action.endswith("."):  # a prefix such as "login."
            where.append("action LIKE ?")
            params.append(action + "%")
        else:
            where.append("action = ?")
            params.append(action)
    if actor:
        where.append("actor = ?")
        params.append(actor)
    if before_id is not None:
        where.append("id < ?")
        params.append(before_id)
    sql = f"SELECT {', '.join(_COLUMNS)} FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    out = []
    for row in store.execute(sql, params):
        d = dict(zip(_COLUMNS, row, strict=True))
        d["id"] = int(d["id"])
        d["detail"] = store.dialect.load_json(d["detail"]) or {}
        out.append(d)
    return out
