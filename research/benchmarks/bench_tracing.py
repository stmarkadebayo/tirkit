"""NFR-1: latency the SDK adds per traced call, writing to SQLite through the batch exporter.

Run: python research/benchmarks/bench_tracing.py [--n 10000]
Times each call with perf_counter_ns: a bare no-op function versus the same function inside
tirkit.span() with a realistic LLM payload (messages in, text out, token usage, cost lookup).
Reports p50/p99 of each and the added latency (traced minus bare, per percentile).
``--redact secrets|strict`` turns on SDK-side redaction, which runs in the exporter thread.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

import tirkit


def pct(values: list[int], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] / 1e6  # ns -> ms


def noop() -> str:
    return "ok"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10_000)
    ap.add_argument(
        "--redact",
        choices=["off", "secrets", "strict"],
        default=None,
        help="SDK-side redaction in the exporter thread (default: none)",
    )
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        client = tirkit.init(path=Path(tmp) / "bench.db", max_queue=args.n * 2, redact=args.redact)
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France? " * 5},
        ]
        for _ in range(500):  # warm-up
            with tirkit.span("warm", kind="llm", model="gpt-4o-mini", input=messages) as s:
                s.output = noop()
        client.flush(60)

        bare: list[int] = []
        for _ in range(args.n):
            t0 = time.perf_counter_ns()
            noop()
            bare.append(time.perf_counter_ns() - t0)

        traced: list[int] = []
        for _ in range(args.n):
            t0 = time.perf_counter_ns()
            with tirkit.span("llm.call", kind="llm", model="gpt-4o-mini", input=messages) as s:
                s.output = noop()
                s.set_usage(input_tokens=120, output_tokens=8)
            traced.append(time.perf_counter_ns() - t0)

        t0 = time.perf_counter()
        client.flush(120)
        drain_s = time.perf_counter() - t0
        stored = client.store.count_spans({"name": "llm.call"})
        stats = client.exporter.stats()
        client.shutdown()

    result = {
        "n": args.n,
        "redact": args.redact,
        "redacted_values": stats.get("redacted", 0),
        "bare_p50_ms": pct(bare, 0.5),
        "bare_p99_ms": pct(bare, 0.99),
        "traced_p50_ms": pct(traced, 0.5),
        "traced_p99_ms": pct(traced, 0.99),
        "added_p50_ms": pct(traced, 0.5) - pct(bare, 0.5),
        "added_p99_ms": pct(traced, 0.99) - pct(bare, 0.99),
        "traced_mean_ms": statistics.mean(traced) / 1e6,
        "stored_spans": stored,
        "dropped": stats["dropped"],
        "drain_after_loop_s": round(drain_s, 3),
        "machine": f"{platform.machine()} {platform.system()} {platform.release()}",
        "python": platform.python_version(),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
