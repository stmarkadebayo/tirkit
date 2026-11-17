"""Prometheus metrics at ``GET /metrics``.

What is exported (no trace, span, project or user labels; every label has a small fixed set of
values):

- ``tirkit_ingest_requests_total{route, outcome, reason}``: ingest requests, accepted or rejected,
  with the reason derived from the status code (``ok``, ``invalid``, ``auth``, ``too_large``,
  ``rate_limited``, ``not_found``, ``error``, ``other``). Routes: ``spans``, ``otlp``, ``media``,
  ``scores``.
- ``tirkit_ingest_spans_total{route}``: spans accepted.
- ``tirkit_ingest_request_duration_seconds{route}``: ingest latency histogram.
- ``tirkit_ingest_buffer_depth``: spans waiting in the server's ingest buffer (0 when there is none;
  an ingest buffer reports through :func:`set_buffer_depth_source`).
- ``tirkit_db_pool_max``, ``tirkit_db_pool_connections{state=in_use|idle}``,
  ``tirkit_db_pool_waiting``: Postgres connection pool (saturation = in_use / max).
- ``tirkit_jobs{state=queued|running}``, ``tirkit_jobs_oldest_ready_age_seconds``: job queue.
- ``tirkit_judge_spend_today_usd``, ``tirkit_judge_spend_usd_total``: LLM-judge spend.
- ``tirkit_db_up``: 1 if the database answered this scrape.

The request path pays for one dictionary lookup per request; ingest requests add two clock reads,
one counter increment and one histogram observation. Queue and spend figures are read from the
database only when Prometheus scrapes. ``prometheus_client`` is imported only when the app is
created.

Several server processes (``tirkit serve --workers N``) share counters through
``$PROMETHEUS_MULTIPROC_DIR``, which ``tirkit serve`` sets up; any scrape sees the sum.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

from tirkit.store import ops

log = logging.getLogger("tirkit.metrics")

_EXACT_ROUTES = {
    ("POST", "/api/v1/ingest"): "spans",
    ("POST", "/v1/traces"): "otlp",
    ("POST", "/api/v1/scores"): "scores",
}
_MEDIA_PREFIX = "/api/v1/media/"
_REASONS = {
    400: "invalid",
    401: "auth",
    403: "auth",
    404: "not_found",
    413: "too_large",
    415: "invalid",
    422: "invalid",
    429: "rate_limited",
}
_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_REFRESH_S = 5.0


def ingest_route(method: str, path: str) -> str | None:
    """The ingest route label for a request, or None for everything else."""
    route = _EXACT_ROUTES.get((method, path))
    if route is None and method == "PUT" and path.startswith(_MEDIA_PREFIX):
        return "media"
    return route


def outcome(status: int) -> tuple[str, str]:
    if status < 400:
        return "accepted", "ok"
    if status >= 500:
        return "rejected", "error"
    return "rejected", _REASONS.get(status, "other")


class NullMetrics:
    """Metrics switched off (``TIRKIT_METRICS=0``) or ``prometheus_client`` not installed."""

    enabled = False
    buffer_source: Callable[[], int] | None = None

    def observe(self, route: str, status: int, seconds: float) -> None:
        return None

    def add_spans(self, route: str, n: int) -> None:
        return None

    def maybe_refresh(self) -> None:
        return None


class Metrics:
    """The app's metrics. One per app; values are per process unless multiprocess mode is on."""

    enabled = True

    def __init__(self, store: Any) -> None:
        from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

        self.store = store
        self.multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR") or None
        self.registry = CollectorRegistry()
        reg = self.registry
        self.requests = Counter(
            "tirkit_ingest_requests",
            "Ingest requests by route, outcome and reason.",
            ["route", "outcome", "reason"],
            registry=reg,
        )
        self.spans = Counter(
            "tirkit_ingest_spans", "Spans accepted by ingest.", ["route"], registry=reg
        )
        self.latency = Histogram(
            "tirkit_ingest_request_duration_seconds",
            "Ingest request latency, from first byte in to response.",
            ["route"],
            buckets=_BUCKETS,
            registry=reg,
        )
        self.buffer = Gauge(
            "tirkit_ingest_buffer_depth",
            "Spans waiting in the server's ingest buffer.",
            registry=reg,
            multiprocess_mode="livesum",
        )
        self.pool = getattr(store, "pool", None)
        if self.pool is not None:
            self.pool_max = Gauge(
                "tirkit_db_pool_max",
                "Maximum connections in the database pool.",
                registry=reg,
                multiprocess_mode="livesum",
            )
            self.pool_conns = Gauge(
                "tirkit_db_pool_connections",
                "Open database pool connections by state.",
                ["state"],
                registry=reg,
                multiprocess_mode="livesum",
            )
            self.pool_waiting = Gauge(
                "tirkit_db_pool_waiting",
                "Requests waiting for a database connection.",
                registry=reg,
                multiprocess_mode="livesum",
            )
        self.db_collector = DatabaseCollector(store)
        if self.multiproc_dir is None:
            from prometheus_client import ProcessCollector

            ProcessCollector(registry=reg)
            reg.register(self.db_collector)
        self.buffer_source: Callable[[], int] | None = None
        self._children: dict[tuple[str, int], tuple[Any, Any]] = {}
        self._spans: dict[str, Any] = {}
        self._next_refresh = 0.0

    def observe(self, route: str, status: int, seconds: float) -> None:
        pair = self._children.get((route, status))
        if pair is None:
            pair = (self.requests.labels(route, *outcome(status)), self.latency.labels(route))
            self._children[(route, status)] = pair
        pair[0].inc()
        pair[1].observe(seconds)

    def add_spans(self, route: str, n: int) -> None:
        child = self._spans.get(route)
        if child is None:
            child = self._spans[route] = self.spans.labels(route)
        child.inc(n)

    def maybe_refresh(self) -> None:
        """Refresh this process's gauges at most every few seconds (for multiprocess mode)."""
        now = time.monotonic()
        if now >= self._next_refresh:
            self._next_refresh = now + _REFRESH_S
            self.refresh()

    def refresh(self) -> None:
        try:
            self.buffer.set(self.buffer_source() if self.buffer_source else 0)
        except Exception:  # noqa: BLE001 - a broken source must not break requests
            log.warning("ingest buffer depth source failed", exc_info=True)
        if self.pool is not None:
            stats = self.pool.get_stats()
            size, available = stats.get("pool_size", 0), stats.get("pool_available", 0)
            self.pool_max.set(stats.get("pool_max", 0))
            self.pool_conns.labels("in_use").set(max(0, size - available))
            self.pool_conns.labels("idle").set(available)
            self.pool_waiting.set(stats.get("requests_waiting", 0))

    def render(self) -> tuple[bytes, str]:
        from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

        self.refresh()
        if self.multiproc_dir is None:
            return generate_latest(self.registry), CONTENT_TYPE_LATEST
        from prometheus_client import multiprocess

        reg = CollectorRegistry()
        multiprocess.MultiProcessCollector(reg, path=self.multiproc_dir)
        reg.register(self.db_collector)
        return generate_latest(reg), CONTENT_TYPE_LATEST


class DatabaseCollector:
    """Job queue and judge spend, read from the database at scrape time."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def collect(self) -> Any:
        from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

        up = GaugeMetricFamily("tirkit_db_up", "1 if the database answered this scrape.")
        try:
            queue = ops.job_queue(self.store)
            spend = ops.judge_spend(self.store)
        except Exception:  # noqa: BLE001 - report as tirkit_db_up 0
            log.warning("metrics: database query failed", exc_info=True)
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up
        jobs = GaugeMetricFamily("tirkit_jobs", "Jobs in the queue by state.", labels=["state"])
        jobs.add_metric(["queued"], queue["queued"])
        jobs.add_metric(["running"], queue["running"])
        yield jobs
        yield GaugeMetricFamily(
            "tirkit_jobs_oldest_ready_age_seconds",
            "How long the oldest ready job has waited (0 when none is waiting).",
            value=queue["oldest_ready_age_s"],
        )
        yield GaugeMetricFamily(
            "tirkit_judge_spend_today_usd",
            "LLM-judge spend today (UTC), all projects.",
            value=spend["today_usd"],
        )
        yield CounterMetricFamily(
            "tirkit_judge_spend_usd",
            "LLM-judge spend over the stored history, all projects.",
            value=spend["total_usd"],
        )


class IngestMetricsMiddleware:
    """Pure ASGI middleware: times ingest requests and counts them by status."""

    def __init__(self, app: Any, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        metrics = self.metrics
        metrics.maybe_refresh()
        route = ingest_route(scope["method"], scope["path"])
        if route is None:
            await self.app(scope, receive, send)
            return
        status = 500

        async def send_status(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_status)
        finally:
            metrics.observe(route, status, time.perf_counter() - start)


def create_metrics(store: Any, enabled: bool) -> Metrics | NullMetrics:
    if not enabled:
        return NullMetrics()
    try:
        return Metrics(store)
    except ImportError:
        log.warning("prometheus-client is not installed: /metrics is unavailable")
        return NullMetrics()


def install(app: FastAPI, store: Any, enabled: bool, token: str | None) -> None:
    """Add the middleware and ``GET /metrics`` (kept out of the OpenAPI document)."""
    metrics = create_metrics(store, enabled)
    app.state.metrics = metrics
    if isinstance(metrics, Metrics):
        app.add_middleware(IngestMetricsMiddleware, metrics=metrics)

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics(request: Request) -> Response:
        if not isinstance(metrics, Metrics):
            raise HTTPException(404, "metrics are disabled")
        if token:
            given = request.headers.get("authorization", "")
            if not secrets.compare_digest(given.encode(), f"Bearer {token}".encode()):
                raise HTTPException(401, "metrics token required")
        body, ctype = metrics.render()
        return Response(body, headers={"Content-Type": ctype})


def count_spans(request: Request, route: str, n: int) -> None:
    """Called by the ingest routes after spans are accepted."""
    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None:
        metrics.add_spans(route, n)


def set_buffer_depth_source(app: FastAPI, source: Callable[[], int] | None) -> None:
    """Let an ingest buffer report its depth (``source()`` returns the number of waiting spans)."""
    app.state.metrics.buffer_source = source
