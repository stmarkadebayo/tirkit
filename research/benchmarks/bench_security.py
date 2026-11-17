"""Cost of the security features on the ingestion path (plan items B3 and B10).

Run: python research/benchmarks/bench_security.py [--n 300] [--postgres-url URL]

1. ``RateLimiter.hit`` per call on each backend (the work added to every ingest request).
2. ``POST /api/v1/ingest`` of 100 spans with the per-key limit on (memory and database backends)
   and off, through the in-process test client: the difference is the limiter's added latency.
3. Server-side and SDK-side redaction cost per span (``secrets`` and ``strict``).

Numbers depend on the machine and on what else is running; the output records both.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tirkit.ratelimit import DatabaseBackend, MemoryBackend, RateLimiter, Rule
from tirkit.redact import Redactor
from tirkit.server.app import create_app
from tirkit.server.settings import Settings
from tirkit.store import SQLiteStore, open_store


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def bench_hits(limiter: RateLimiter, n: int) -> dict[str, float]:
    times = []
    for i in range(n):
        t0 = time.perf_counter_ns()
        limiter.hit(f"key{i % 5}", cost=100)
        times.append(time.perf_counter_ns() - t0)
    return {"p50_us": pct(times, 0.5) / 1e3, "p99_us": pct(times, 0.99) / 1e3}


SPAN = {
    "trace_id": "t",
    "name": "llm.call",
    "kind": "llm",
    "model": "gpt-4o-mini",
    "input": [{"role": "user", "content": "What is the capital of France? " * 5}],
    "output": "Paris. Contact me at ana@example.com or +1 415 555 0134.",
    "usage": {"input_tokens": 120, "output_tokens": 8},
}


def bench_ingest(
    settings_kw: dict[str, Any], n: int, redact_mode: str | None = None
) -> dict[str, float]:
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings()
        s.database_url = str(Path(tmp) / "b.db")
        s.media_dir = Path(tmp) / "media"
        s.secure_cookies = False
        s.web_dir = None
        s.embedded_worker = False
        for k, v in settings_kw.items():
            setattr(s, k, v)
        with TestClient(create_app(s)) as c:
            r = c.post(
                "/api/setup", json={"email": "b@example.com", "password": "bench-password-1"}
            )
            pid = r.json()["project_id"]
            c.headers["X-CSRF-Token"] = c.get("/api/auth/me").json()["csrf"]
            key = c.post(f"/api/projects/{pid}/keys", json={}).json()
            if redact_mode:
                c.put(f"/api/projects/{pid}/settings", json={"redact_mode": redact_mode})
            c.cookies.clear()
            auth = (key["public_key"], key["secret_key"])
            times = []
            for i in range(n + 20):
                body = json.dumps(
                    {
                        "spans": [
                            {**SPAN, "span_id": f"s{i}-{j}", "trace_id": f"t{i}"}
                            for j in range(100)
                        ]
                    }
                )
                t0 = time.perf_counter_ns()
                resp = c.post(
                    "/api/v1/ingest",
                    content=body,
                    auth=auth,
                    headers={"content-type": "application/json"},
                )
                dt = time.perf_counter_ns() - t0
                assert resp.status_code == 200, resp.text
                if i >= 20:  # warm-up
                    times.append(dt)
    return {
        "p50_ms": pct(times, 0.5) / 1e6,
        "p95_ms": pct(times, 0.95) / 1e6,
        "mean_ms": statistics.mean(times) / 1e6,
    }


def bench_redaction(mode: str, n: int = 5000) -> dict[str, float]:
    times = []
    for _ in range(n):
        r = Redactor(mode)
        t0 = time.perf_counter_ns()
        r.obj({"input": SPAN["input"], "output": SPAN["output"]})
        times.append(time.perf_counter_ns() - t0)
    return {"p50_us": pct(times, 0.5) / 1e3, "p99_us": pct(times, 0.99) / 1e3}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--postgres-url", default=None)
    args = ap.parse_args()
    out: dict[str, Any] = {
        "machine": f"{platform.machine()} {platform.system()} {platform.release()}"
    }
    rule = Rule(1e9, 1.0, burst=1e9)
    out["limiter_hit_memory"] = bench_hits(RateLimiter("b", rule, MemoryBackend()), 20_000)
    with tempfile.TemporaryDirectory() as tmp:
        st = SQLiteStore(Path(tmp) / "rl.db")
        st.init_schema()
        out["limiter_hit_sqlite"] = bench_hits(RateLimiter("b", rule, DatabaseBackend(st)), 5_000)
        st.close()
    if args.postgres_url:
        pg = open_store(args.postgres_url)
        pg.init_schema()
        out["limiter_hit_postgres_local"] = bench_hits(
            RateLimiter("b", rule, DatabaseBackend(pg)), 2_000
        )
        pg.close()
    # Alternate the variants so slow drift (other processes) hits each equally.
    variants = {
        "ingest_limit_off": {"ingest_spans_per_s": 0},
        "ingest_limit_memory": {"ingest_spans_per_s": 1e9, "rate_limit_backend": "memory"},
        "ingest_limit_database": {"ingest_spans_per_s": 1e9, "rate_limit_backend": "database"},
    }
    runs: dict[str, list[dict[str, float]]] = {k: [] for k in variants}
    for _ in range(3):
        for name, kw in variants.items():
            runs[name].append(bench_ingest(kw, args.n))
    for name, rs in runs.items():
        out[name + "_100_spans_per_request"] = {
            k: round(statistics.median(r[k] for r in rs), 3) for k in rs[0]
        } | {"runs": len(rs)}
    # The server-side redaction mode is part of every ingest request (new projects: secrets).
    modes: dict[str, list[dict[str, float]]] = {m: [] for m in ("off", "secrets", "strict")}
    for _ in range(3):
        for m in modes:
            modes[m].append(bench_ingest({"ingest_spans_per_s": 0}, args.n, redact_mode=m))
    for m, rs in modes.items():
        out[f"ingest_redact_{m}_100_spans_per_request"] = {
            k: round(statistics.median(r[k] for r in rs), 3) for k in rs[0]
        } | {"runs": len(rs)}
    out["redaction_per_span_secrets"] = bench_redaction("secrets")
    out["redaction_per_span_strict"] = bench_redaction("strict")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
