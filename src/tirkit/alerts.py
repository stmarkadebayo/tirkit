"""Threshold alerts on error rate, latency, cost or a score average (SPEC FR-10).

A rule names a metric over a trailing window and a threshold. The worker evaluates rules every
minute and notifies on state changes (ok → firing, firing → ok) through a webhook (Slack-compatible
``{"text": ...}`` JSON) or email (SMTP settings from ``TIRKIT_SMTP_*``). Every evaluation that
changes state is logged in ``alert_events``.
"""

from __future__ import annotations

import logging
import os
import smtplib
import time
import uuid
from email.message import EmailMessage
from typing import Any

from tirkit.store import SQLStore

log = logging.getLogger("tirkit.alerts")

METRICS = {"error_rate", "p95_latency_ms", "cost_usd", "trace_count", "score_mean"}


def _validate(rule: dict[str, Any], channel: dict[str, Any]) -> None:
    if rule.get("metric") not in METRICS:
        raise ValueError(f"metric must be one of {sorted(METRICS)}")
    if rule.get("op") not in (">", "<"):
        raise ValueError("op must be > or <")
    float(rule["threshold"])
    if float(rule.get("window_s", 3600)) <= 0:
        raise ValueError("window_s must be positive")
    if rule["metric"] == "score_mean" and not rule.get("score_name"):
        raise ValueError("score_mean alerts need score_name")
    kind = channel.get("type")
    if kind == "webhook":
        url = str(channel.get("url", ""))
        if not url.startswith(("https://", "http://")):
            raise ValueError("webhook url must be http(s)")
    elif kind == "email":
        if "@" not in str(channel.get("to", "")):
            raise ValueError("email channel needs 'to'")
    else:
        raise ValueError("channel type must be webhook or email")


def measure(store: SQLStore, rule: dict[str, Any], now: float | None = None) -> float | None:
    """Current value of the rule's metric over its window (None when there is no data)."""
    now = now or time.time()
    since = now - float(rule.get("window_s", 3600))
    m = rule["metric"]
    if m == "score_mean":
        rows = store.execute(
            "SELECT AVG(value) FROM scores WHERE project_id = ? AND name = ? AND created >= ? "
            "AND value IS NOT NULL AND experiment_id IS NULL",
            [store.project_id, rule["score_name"], since],
        )
        return float(rows[0][0]) if rows and rows[0][0] is not None else None
    rows = store.execute(
        "SELECT COUNT(*), SUM(has_error), SUM(cost_usd) FROM traces WHERE project_id = ? "
        "AND start_time >= ? AND start_time < ?",
        [store.project_id, since, now],
    )
    n, errors, cost = rows[0]
    n = int(n or 0)
    if m == "trace_count":
        return float(n)
    if m == "cost_usd":
        return float(cost or 0.0)
    if n == 0:
        return None
    if m == "error_rate":
        return float(errors or 0) / n
    durs = sorted(
        float(r[0])
        for r in store.execute(
            "SELECT duration_ms FROM traces WHERE project_id = ? AND start_time >= ? AND "
            "start_time < ? AND duration_ms IS NOT NULL ORDER BY start_time DESC LIMIT 100000",
            [store.project_id, since, now],
        )
    )
    if not durs:
        return None
    return durs[min(len(durs) - 1, int(0.95 * len(durs)))]


def notify(channel: dict[str, Any], subject: str, text: str) -> None:
    if channel["type"] == "webhook":
        import httpx

        r = httpx.post(channel["url"], json={"text": f"{subject}\n{text}"}, timeout=10)
        r.raise_for_status()
        return
    host = os.environ.get("TIRKIT_SMTP_HOST")
    if not host:
        raise RuntimeError(
            "email alerts need TIRKIT_SMTP_HOST (and TIRKIT_SMTP_FROM, user, password)"
        )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("TIRKIT_SMTP_FROM", "tirkit@localhost")
    msg["To"] = channel["to"]
    msg.set_content(text)
    with smtplib.SMTP(host, int(os.environ.get("TIRKIT_SMTP_PORT", "587")), timeout=15) as smtp:
        if os.environ.get("TIRKIT_SMTP_STARTTLS", "1") == "1":
            smtp.starttls()
        if os.environ.get("TIRKIT_SMTP_USER"):
            smtp.login(os.environ["TIRKIT_SMTP_USER"], os.environ.get("TIRKIT_SMTP_PASSWORD", ""))
        smtp.send_message(msg)


class Alerts:
    def __init__(self, store: SQLStore) -> None:
        self.store = store
        self.pid = store.project_id

    def create(self, name: str, rule: dict[str, Any], channel: dict[str, Any]) -> str:
        _validate(rule, channel)
        aid = "al_" + uuid.uuid4().hex[:10]
        self.store.execute(
            "INSERT INTO alerts (project_id, alert_id, name, rule, channel, active, state, "
            "created) VALUES (?, ?, ?, ?, ?, 1, 'ok', ?)",
            [self.pid, aid, name, self.store._json(rule), self.store._json(channel), time.time()],
        )
        return aid

    def items(self) -> list[dict[str, Any]]:
        rows = self.store.execute(
            "SELECT alert_id, name, rule, channel, active, state, last_value, last_checked, "
            "last_fired, created FROM alerts WHERE project_id = ? ORDER BY created",
            [self.pid],
        )
        keys = "alert_id name rule channel active state last_value last_checked last_fired created"
        out = []
        for r in rows:
            d = dict(zip(keys.split(), r, strict=True))
            d["rule"] = self.store.dialect.load_json(d["rule"])
            d["channel"] = self.store.dialect.load_json(d["channel"])
            d["active"] = bool(d["active"])
            out.append(d)
        return out

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.store.execute(
            "SELECT alert_id, at, state, value, delivered, error FROM alert_events "
            "WHERE project_id = ? ORDER BY at DESC LIMIT ?",
            [self.pid, limit],
        )
        keys = "alert_id at state value delivered error".split()
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def delete(self, alert_id: str) -> None:
        self.store.execute(
            "DELETE FROM alerts WHERE project_id = ? AND alert_id = ?", [self.pid, alert_id]
        )

    def check(self, alert: dict[str, Any], now: float | None = None) -> str:
        now = now or time.time()
        rule = alert["rule"]
        value = measure(self.store, rule, now)
        threshold = float(rule["threshold"])
        firing = value is not None and (
            value > threshold if rule["op"] == ">" else value < threshold
        )
        state = "firing" if firing else "ok"
        self.store.execute(
            "UPDATE alerts SET state = ?, last_value = ?, last_checked = ? WHERE project_id = ? "
            "AND alert_id = ?",
            [state, value, now, self.pid, alert["alert_id"]],
        )
        if state != alert["state"]:
            subject = f"[Tirkit] {alert['name']}: {'FIRING' if firing else 'resolved'}"
            text = (
                f"{rule['metric']} over the last {int(rule.get('window_s', 3600)) // 60} min is "
                f"{value if value is None else round(value, 4)} "
                f"(threshold {rule['op']} {threshold})."
                f"\nProject {self.pid}."
            )
            delivered, error = 1, None
            try:
                notify(alert["channel"], subject, text)
            except Exception as exc:  # noqa: BLE001 - recorded on the event
                delivered, error = 0, f"{type(exc).__name__}: {exc}"
            self.store.execute(
                "INSERT INTO alert_events (project_id, alert_id, at, state, value, delivered, "
                "error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [self.pid, alert["alert_id"], now, state, value, delivered, error],
            )
            if firing:
                self.store.execute(
                    "UPDATE alerts SET last_fired = ? WHERE project_id = ? AND alert_id = ?",
                    [now, self.pid, alert["alert_id"]],
                )
        return state


def check_all(root: SQLStore) -> None:
    for (pid,) in root.execute("SELECT DISTINCT project_id FROM alerts WHERE active = 1"):
        a = Alerts(root.for_project(pid))
        for alert in a.items():
            if alert["active"]:
                try:
                    a.check(alert)
                except Exception:  # noqa: BLE001
                    log.warning("alert %s failed", alert["alert_id"], exc_info=True)
