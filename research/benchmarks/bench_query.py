"""NFR-6: trace-list and trace-open latency (p50/p95) through the HTTP API with 1M or 10M spans.

    python research/benchmarks/bench_query.py --db postgresql://tirkit:tirkit@127.0.0.1:55432/tirkit_bench --seed
    python research/benchmarks/bench_query.py --db postgresql://... --seed --spans 10000000
    python research/benchmarks/bench_query.py --db postgresql://... (reuses the seeded data)
    python research/benchmarks/bench_query.py --db /tmp/bench.db --seed --spans 100000  (SQLite)

Seeding writes --spans spans (five-span traces over 30 days, 500 users, sessions = traces / 10,
2% errors) through the store, plus one --big-trace span trace (default 10,000 spans, ~3 KB of
input each). Each list query runs --repeat times via `GET /traces` (the same request the UI
makes, including the total count). Trace-open queries time `GET /traces/{id}` (the light tree
the UI loads), the same with `full=1`, and `GET /traces/{id}/spans/{span_id}` (one full span).
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
from pathlib import Path
from typing import Any

import httpx

DAY = 86400


BIG_TRACE_ID = "b" * 32


def is_postgres(db: str) -> bool:
    return db.startswith(("postgres://", "postgresql://"))


def open_bench_store(db: str, project_id: str = "bench") -> Any:
    from tirkit.store import open_store

    return open_store(db, project_id)


def seed(db: str, n_spans: int, big_trace: int) -> None:
    from tirkit.tracing import Span

    if is_postgres(db):
        import psycopg

        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    else:
        for suffix in ("", "-wal", "-shm"):
            Path(db + suffix).unlink(missing_ok=True)
    st = open_bench_store(db)
    st.init_schema()
    rng = random.Random(0)
    now = time.time()
    batch: list[Span] = []
    t0 = time.time()
    n_traces = n_spans // 5
    n_sessions = max(1, n_traces // 10)
    for i in range(n_traces):
        if i and i % 100_000 == 0:
            rate = i * 5 / (time.time() - t0)
            print(f"  {i * 5:,} spans ({rate:,.0f}/s)", file=sys.stderr, flush=True)
        start = now - rng.random() * 30 * DAY
        tid, root = uuid.uuid4().hex, uuid.uuid4().hex[:16]
        user, session = f"user-{rng.randrange(500)}", f"session-{rng.randrange(n_sessions)}"
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
    if big_trace:
        seed_big_trace(st, big_trace, now - DAY, rng)
    st.execute("ANALYZE")
    print(
        f"seeded {st.count_spans()} spans, {st.count_traces()} traces in {time.time() - t0:.0f}s",
        file=sys.stderr,
    )
    st.close()


def seed_big_trace(st: Any, n: int, start: float, rng: random.Random) -> None:
    """One agent trace with ``n`` spans: tool and LLM calls under a few hundred sub-agents."""
    from tirkit.tracing import Span

    root = Span(
        name="big-agent",
        trace_id=BIG_TRACE_ID,
        span_id="b" * 16,
        start=start,
        end=start + 600,
        kind="agent",
        input={"task": "t" * 3000},
        output="done",
    )
    spans = [root]
    for i in range(n - 1):
        parent = spans[rng.randrange(min(len(spans), 300))]
        s = Span(
            name=f"step-{i % 50}",
            trace_id=BIG_TRACE_ID,
            span_id=f"{i:016x}",
            parent_id=parent.span_id,
            start=start + rng.random() * 590,
            kind=rng.choice(["llm", "tool", "retriever"]),
            model="gpt-4o-mini",
            input=[{"role": "user", "content": "q" * 3000}],
            output="a" * 500,
            usage={"input_tokens": 800, "output_tokens": 120},
            cost_usd=0.0002,
        )
        s.end = s.start + rng.random() * 5
        spans.append(s)
    for i in range(0, len(spans), 2000):
        st.save_spans(spans[i : i + 2000])


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
    ap.add_argument("--big-trace", type=int, default=10_000, help="spans in the one big trace")
    args = ap.parse_args()
    if args.seed:
        seed(args.db, args.spans, args.big_trace)

    from tirkit.accounts import Accounts

    st = open_bench_store(args.db)
    acc = Accounts(st)
    st.execute("DELETE FROM api_keys WHERE project_id = 'bench'")
    st.execute(
        "INSERT INTO projects (project_id, name, created) VALUES ('bench', 'bench', 0) "
        "ON CONFLICT DO NOTHING"
    )
    pk, sk = acc.create_api_key("bench")
    n_spans, n_traces = st.count_spans(), st.count_traces()
    small = st.query_traces({"name": "search"}, limit=1)
    big = st.get_trace(BIG_TRACE_ID)
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
        requests: dict[str, tuple[str, dict[str, Any]]] = {
            label: ("/api/projects/bench/traces", {"limit": 50, **params})
            for label, params in queries.items()
        }
        trace_url = "/api/projects/bench/traces/{}"
        if small:
            tid = small[0].trace_id
            requests["open 5-span trace (light)"] = (trace_url.format(tid), {})
        if big:
            url = trace_url.format(BIG_TRACE_ID)
            requests[f"open {big.n_spans}-span trace (light)"] = (url, {})
            requests[f"open {big.n_spans}-span trace (full=1)"] = (url, {"full": "1"})
            requests["one full span of the big trace"] = (f"{url}/spans/{0:016x}", {})
        results = {}
        for label, (path, params) in requests.items():
            times = []
            size = 0
            for k in range(args.repeat + 3):
                t0 = time.perf_counter()
                r = c.get(path, params=params)
                r.raise_for_status()
                if k >= 3:  # warm-up excluded
                    times.append((time.perf_counter() - t0) * 1000)
                size = len(r.content)
            results[label] = {
                "p50_ms": round(pct(times, 0.5), 1),
                "p95_ms": round(pct(times, 0.95), 1),
                "response_kb": round(size / 1024),
            }
        print(
            json.dumps(
                {
                    "spans": n_spans,
                    "traces": n_traces,
                    "repeat": args.repeat,
                    "queries": results,
                    "machine": f"{platform.machine()} {platform.system()}",
                    "db": "postgres (same machine)" if is_postgres(args.db) else "sqlite",
                    "load_avg": [round(x, 2) for x in os.getloadavg()],
                },
                indent=2,
            )
        )
    finally:
        server.terminate()
        server.wait(10)


if __name__ == "__main__":
    main()
