"""NFR-5: sustained server ingestion throughput (spans/s) without dropping data.

Run against a scratch Postgres database (it is wiped):
    python research/benchmarks/bench_ingest.py --db postgresql://tirkit:tirkit@127.0.0.1:55432/tirkit_bench

Starts `tirkit serve` in a subprocess, creates an admin and an API key, then several client threads
post batches of realistic chat spans (≈1 KB each, 5 spans per trace) for --seconds. Reports
accepted spans/s and checks that the database holds exactly the number of spans sent.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_trace(i: int) -> list[dict]:
    trace_id = uuid.uuid4().hex
    root = uuid.uuid4().hex[:16]
    now = time.time()
    spans = [
        {
            "name": "support-agent",
            "trace_id": trace_id,
            "span_id": root,
            "kind": "agent",
            "start": now,
            "end": now + 1.2,
            "input": {"question": f"How long does shipping take for order {i}?"},
            "output": "Standard shipping takes 3 to 5 business days.",
            "user_id": f"user-{i % 500}",
            "session_id": f"session-{i % 2000}",
        }
    ]
    for j, kind in enumerate(["retrieval", "tool", "llm", "llm"]):
        spans.append(
            {
                "name": f"step-{kind}",
                "trace_id": trace_id,
                "span_id": uuid.uuid4().hex[:16],
                "parent_id": root,
                "kind": kind,
                "start": now + 0.1 * j,
                "end": now + 0.1 * j + 0.3,
                "model": "gpt-4o-mini" if kind == "llm" else None,
                "input": {
                    "messages": [
                        {"role": "system", "content": "You are a helpful support agent. " * 4},
                        {"role": "user", "content": f"How long does shipping take for order {i}?"},
                    ]
                },
                "output": {"role": "assistant", "content": "Standard shipping takes 3 to 5 days."},
                "usage": {"input_tokens": 180, "output_tokens": 14} if kind == "llm" else {},
            }
        )
    return spans


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--batch", type=int, default=100, help="spans per request")
    ap.add_argument("--workers", type=int, default=2, help="uvicorn worker processes")
    args = ap.parse_args()

    import psycopg

    with psycopg.connect(args.db, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    port = free_port()
    env = {**os.environ, "TIRKIT_INSECURE_COOKIES": "1", "TIRKIT_MEDIA_DIR": "/tmp/tirkit-bench-media"}
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tirkit.cli",
            "serve",
            "--port",
            str(port),
            "--database-url",
            args.db,
            "--workers",
            str(args.workers),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            try:
                if httpx.get(f"{base}/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        c = httpx.Client(base_url=base)
        pid = c.post(
            "/api/setup", json={"email": "b@example.com", "password": "benchmark-pass"}
        ).json()["project_id"]
        c.headers["X-CSRF-Token"] = c.get("/api/auth/me").json()["csrf"]
        keys = c.post(f"/api/projects/{pid}/keys", json={}).json()

        sent = [0] * args.clients
        failures = [0] * args.clients
        stop = time.time() + args.seconds

        def worker(k: int) -> None:
            cl = httpx.Client(
                base_url=base, auth=(keys["public_key"], keys["secret_key"]), timeout=60
            )
            i = k * 10_000_000
            while time.time() < stop:
                spans: list[dict] = []
                while len(spans) < args.batch:
                    spans.extend(make_trace(i))
                    i += 1
                r = cl.post(
                    "/api/v1/ingest",
                    content=json.dumps({"spans": spans}),
                    headers={"content-type": "application/json"},
                )
                if r.status_code == 200:
                    sent[k] += len(spans)
                else:
                    failures[k] += 1

        t0 = time.time()
        threads = [threading.Thread(target=worker, args=(k,)) for k in range(args.clients)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.time() - t0
        with psycopg.connect(args.db) as conn:
            stored = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
        total = sum(sent)
        print(
            json.dumps(
                {
                    "spans_sent": total,
                    "spans_stored": stored,
                    "dropped": total - stored,
                    "failed_requests": sum(failures),
                    "seconds": round(elapsed, 1),
                    "spans_per_s": round(total / elapsed, 1),
                    "clients": args.clients,
                    "batch": args.batch,
                    "uvicorn_workers": args.workers,
                    "machine": f"{platform.machine()} {platform.system()}",
                    "db": "postgres 16 (docker, same machine)",
                },
                indent=2,
            )
        )
    finally:
        server.terminate()
        server.wait(10)


if __name__ == "__main__":
    main()
