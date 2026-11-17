"""Restore drill: back up, wipe, restore, then compare every table and a sample of media files.

Runs the real ``tirkit backup`` and ``tirkit restore`` commands. Use it in CI and on a scratch copy
of a real installation, never against the live database: it deletes the database it is given.

    python scripts/restore_drill.py --database-url /tmp/drill.db --media-dir /tmp/drill-media \\
        --seed 1000 --yes
    python scripts/restore_drill.py --database-url postgresql://u:p@localhost/scratch \\
        --media-dir /tmp/m --seed 1000 --yes

Exit code 0 when everything matches, 1 otherwise. Prints how long backup and restore took.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from tirkit.media import LocalMediaStore
from tirkit.store import open_store
from tirkit.store.backup import fingerprint
from tirkit.tracing import Span


def seed(url: str, media_dir: Path, n_traces: int) -> None:
    """``n_traces`` traces of three spans each, one media file per ten traces, scores."""
    store = open_store(url)
    store.init_schema()
    media = LocalMediaStore(media_dir)
    now = time.time() - n_traces
    batch: list[Span] = []
    for i in range(n_traces):
        ref = None
        if i % 10 == 0:
            ref = media.put(f"drill-media-{i}".encode() * 64, "image/png")
            store.record_media(ref["sha256"], "image/png", ref["size"])
        root = Span(
            name="agent",
            start=now + i,
            end=now + i + 1.2,
            input={"question": f"drill question {i}", "image": ref},
            output=f"drill answer {i}",
            session_id=f"s{i % 50}",
            user_id=f"u{i % 20}",
            tags=["drill"],
        )
        llm = Span(
            name="llm",
            kind="llm",
            trace_id=root.trace_id,
            parent_id=root.span_id,
            start=now + i + 0.1,
            end=now + i + 1.0,
            model="gpt-4o-mini",
            input=[{"role": "user", "content": f"drill question {i}"}],
            output={"role": "assistant", "content": f"drill answer {i}"},
            usage={"input_tokens": 50 + i % 7, "output_tokens": 20},
            cost_usd=0.00002 * (1 + i % 5),
        )
        tool = Span(
            name="search",
            kind="tool",
            trace_id=root.trace_id,
            parent_id=root.span_id,
            start=now + i + 0.02,
            end=now + i + 0.08,
            input={"query": f"q{i}"},
            output=[f"doc{i}"],
        )
        batch += [root, llm, tool]
        if len(batch) >= 3000:
            store.save_spans(batch)
            batch = []
        if i % 4 == 0:
            store.save_score("helpful", float(i % 2), trace_id=root.trace_id)
    if batch:
        store.save_spans(batch)
    store.close()


def wipe(url: str, media_dir: Path) -> None:
    if url.startswith(("postgres://", "postgresql://")):
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    else:
        for suffix in ("", "-wal", "-shm"):
            Path(url + suffix).unlink(missing_ok=True)
    shutil.rmtree(media_dir, ignore_errors=True)


def media_sample(media_dir: Path, k: int, rng: random.Random) -> dict[str, str]:
    files = sorted(p for p in media_dir.rglob("*") if p.is_file())
    chosen = rng.sample(files, min(k, len(files)))
    return {
        str(p.relative_to(media_dir)): hashlib.sha256(p.read_bytes()).hexdigest() for p in chosen
    }


def tirkit(*args: str) -> float:
    started = time.perf_counter()
    cmd = [sys.executable, "-m", "tirkit.cli", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"`tirkit {args[0]}` failed with exit code {proc.returncode}")
    return time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--media-dir", required=True, type=Path)
    ap.add_argument("--seed", type=int, default=0, help="Seed this many traces first.")
    ap.add_argument("--media-sample", type=int, default=50, help="Media files to re-hash.")
    ap.add_argument("--work", type=Path, help="Where to write the backup (default: a temp dir).")
    ap.add_argument("--yes", action="store_true", help="Confirm the database may be wiped.")
    args = ap.parse_args(argv)
    if not args.yes:
        ap.error("the drill deletes the database it is given; pass --yes on a scratch database")

    url, media_dir = args.database_url, args.media_dir
    if args.seed:
        seed(url, media_dir, args.seed)
    store = open_store(url)
    before: dict[str, Any] = fingerprint(store, sample=None)
    store.close()
    rng = random.Random(0)
    media_before = media_sample(media_dir, args.media_sample, rng) if media_dir.exists() else {}

    work = args.work or Path(tempfile.mkdtemp(prefix="tirkit-drill-"))
    out = work / "backup"
    t_backup = tirkit("backup", str(out), "--database-url", url, "--media-dir", str(media_dir))
    wipe(url, media_dir)
    t_restore = tirkit("restore", str(out), "--database-url", url, "--media-dir", str(media_dir))

    store = open_store(url)
    after = fingerprint(store, sample=None)
    store.close()
    media_after = {
        name: hashlib.sha256((media_dir / name).read_bytes()).hexdigest()
        if (media_dir / name).is_file()
        else "missing"
        for name in media_before
    }
    bad_tables = sorted(t for t in before if before[t] != after.get(t))
    bad_media = sorted(n for n in media_before if media_before[n] != media_after[n])
    rows = sum(t["rows"] for t in before.values())
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(
        f"drill: {len(before)} tables, {rows} rows, {len(media_before)} media files re-hashed; "
        f"backup {t_backup:.2f} s, restore {t_restore:.2f} s, backup size {size} bytes"
    )
    if bad_tables or bad_media:
        print(f"MISMATCH tables={bad_tables} media={bad_media}", file=sys.stderr)
        return 1
    print("drill passed: every table and every sampled media file matches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
