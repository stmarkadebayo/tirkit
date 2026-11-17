"""NFR-6: trace-list query latency (p50/p95) through the HTTP API with 1M stored spans.

    python research/benchmarks/bench_query.py --db postgresql://tirkit:tirkit@127.0.0.1:55432/tirkit_bench --seed
    python research/benchmarks/bench_query.py --db postgresql://... (reuses the seeded data)

Seeding writes 1,000,000 spans (200,000 five-span traces over 30 days, 500 users, 20,000
sessions, 2% errors) through the store. Each query runs --repeat times via `GET /traces` (the
same request the UI makes, including the total count).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import socket
import subprocess
import sys
import time
import uuid

import httpx

DAY = 86400


def seed(db: str, n_spans: int) -> None:
    import psycopg

    from tirkit.store import PostgresStore
    from tirkit.tracing import Span

    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    st = PostgresStore(db, project_id="bench")
    st.init_schema()
    rng = random.Random(0)
    now = time.time()
    batch: list[Span] = []
    t0 = time.time()
    for i in range(n_spans // 5):
        start = now - rng.random() * 30 * DAY
        tid, root = uuid.uuid4().hex, uuid.uuid4().hex[:16]
        user, session = f"user-{rng.randrange(500)}", f"session-{rng.randrange(20000)}"
        name = rng.choice(["support-agent", "search", "summarise", "classify"])
        err = rng.random() < 0.02
        batch.append(
            Span(
                name=name,
                trace_id=tid,
                span_id=root,
                start=start,
                end=start + rng.uniform(0.2, 4),
                kind="agent",
                input={"question": f"question {i}"},
                output="answer",
                user_id=user,
                session_id=session,
                status="error" if err else "ok",
            )
        )
        for j in range(4):
            batch.append(
                Span(
                    name=f"step-{j}",
                    trace_id=tid,
                    span_id=uuid.uuid4().hex[:16],
                    parent_id=root,
                    start=start + 0.05 * j,
                    end=start + 0.05 * j + 0.1,
                    kind="llm",
                    model="gpt-4o-mini",
                    usage={"input_tokens": 200, "output_tokens": 20},
                    cost_usd=0.00004,
                    user_id=user,
                    session_id=session,
                )
            )
        if len(batch) >= 2000:
            st.save_spans(batch)
            batch = []
    if batch:
        st.save_spans(batch)
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("ANALYZE")
    print(f"seeded {st.count_spans()} spans, {st.count_traces()} traces in {time.time() - t0:.0f}s")
    st.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--spans", type=int, default=1_000_000)
    ap.add_argument("--repeat", type=int, default=50)
    args = ap.parse_args()
    if args.seed:
        seed(args.db, args.spans)

    import psycopg

    from tirkit.accounts import Accounts
    from tirkit.store import PostgresStore

    st = PostgresStore(args.db)
    acc = Accounts(st)
    with psycopg.connect(args.db, autocommit=True) as conn:
        conn.execute("DELETE FROM api_keys WHERE project_id = 'bench'")
        conn.execute(
            "INSERT INTO projects (project_id, name, created) VALUES ('bench', 'bench', 0) "
            "ON CONFLICT DO NOTHING"
        )
    pk, sk = acc.create_api_key("bench")
    n_spans, n_traces = (
        st.for_project("bench").count_spans(),
        st.for_project("bench").count_traces(),
    )
    st.close()

    port = free_port()
    env = {**os.environ, "TIRKIT_INSECURE_COOKIES": "1"}
    server = subprocess.Popen(
        [sys.executable, "-m", "tirkit.cli", "serve", "--port", str(port), "--database-url", args.db],
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
        c = httpx.Client(base_url=base, auth=(pk, sk), timeout=60)
        now = time.time()
        queries = {
            "newest 50 + total": {},
            "user filter": {"user_id": "user-42"},
            "session filter": {"session_id": "session-123"},
            "name filter": {"name": "classify"},
            "errors only": {"error": "true"},
            "last hour": {"start_after": now - 3600},
            "page 100 (offset 4950)": {"offset": 4950},
            "min latency 3s": {"min_duration_ms": 3000},
        }
        results = {}
        for label, params in queries.items():
            times = []
            for k in range(args.repeat + 3):
                t0 = time.perf_counter()
                r = c.get("/api/projects/bench/traces", params={"limit": 50, **params})
                r.raise_for_status()
                if k >= 3:  # warm-up excluded
                    times.append((time.perf_counter() - t0) * 1000)
            results[label] = {
                "p50_ms": round(pct(times, 0.5), 1),
                "p95_ms": round(pct(times, 0.95), 1),
            }
        print(
            json.dumps(
                {
                    "spans": n_spans,
                    "traces": n_traces,
                    "repeat": args.repeat,
                    "queries": results,
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
