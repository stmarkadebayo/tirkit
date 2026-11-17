"""Cost of the Prometheus ingest middleware per request.

Calls an empty ASGI app directly, with and without ``IngestMetricsMiddleware`` in front, for an
ingest path (timed and counted) and a non-ingest path (one dictionary lookup), plus
``count_spans``. No network, no server: this isolates what the metrics add to each request.

    python research/benchmarks/bench_metrics.py [--n 200000] [--runs 3]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import statistics
import sys
import tempfile
import time
from typing import Any


async def empty_app(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


async def receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def send(message: Any) -> None:
    return None


async def loop(app: Any, scope: dict[str, Any], n: int) -> float:
    start = time.perf_counter()
    for _ in range(n):
        await app(scope, receive, send)
    return (time.perf_counter() - start) / n * 1e6  # µs per request


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200_000)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--multiprocess", action="store_true", help="file-backed counters")
    args = ap.parse_args()
    if args.multiprocess:
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = tempfile.mkdtemp(prefix="bench-prom-")

    from tirkit.server.prometheus import IngestMetricsMiddleware, Metrics
    from tirkit.store import SQLiteStore

    store = SQLiteStore(":memory:")
    store.init_schema()
    metrics = Metrics(store)
    wrapped = IngestMetricsMiddleware(empty_app, metrics)
    ingest = {"type": "http", "method": "POST", "path": "/api/v1/ingest"}
    other = {"type": "http", "method": "GET", "path": "/api/projects/p/traces"}

    results: dict[str, list[float]] = {"bare": [], "ingest": [], "other": [], "count_spans": []}
    for _ in range(args.runs):
        results["bare"].append(asyncio.run(loop(empty_app, ingest, args.n)))
        results["ingest"].append(asyncio.run(loop(wrapped, ingest, args.n)))
        results["other"].append(asyncio.run(loop(wrapped, other, args.n)))
        start = time.perf_counter()
        for _ in range(args.n):
            metrics.add_spans("spans", 100)
        results["count_spans"].append((time.perf_counter() - start) / args.n * 1e6)

    print(
        f"python {sys.version.split()[0]}, {platform.machine()}, n={args.n}, runs={args.runs}, "
        f"multiprocess={args.multiprocess}"
    )
    bare = statistics.median(results["bare"])
    for name, vals in results.items():
        med = statistics.median(vals)
        extra = f"  (+{med - bare:.2f} µs over bare)" if name in ("ingest", "other") else ""
        print(f"{name:12s} median {med:.2f} µs/request  runs {[round(v, 2) for v in vals]}{extra}")


if __name__ == "__main__":
    main()
