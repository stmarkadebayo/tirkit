"""`tirkit backup` / `tirkit restore` (Phase F): SQLite online backup, Postgres via pg_dump, media
manifest by content hash, and the restore drill (backup, wipe, restore, compare)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tirkit.datasets import Dataset, Example
from tirkit.media import LocalMediaStore
from tirkit.store import SQLiteStore, open_store
from tirkit.store.backup import BackupError, backup, fingerprint, restore, verify
from tirkit.tracing import Span


def seed(store: Any, media: LocalMediaStore, n_traces: int = 20) -> list[str]:
    """Spans with searchable text, a dataset, scores and media files. Returns media hashes."""
    now = time.time()
    hashes = []
    for i in range(n_traces):
        blob = f"image-{i}".encode() * 50
        ref = media.put(blob, "image/png")
        store.record_media(ref["sha256"], "image/png", ref["size"])
        hashes.append(ref["sha256"])
        root = Span(
            name="agent",
            start=now + i,
            end=now + i + 1,
            input={"q": f"walrus question {i}", "image": ref},
            output=f"answer {i}",
            session_id=f"s{i % 3}",
            tags=["drill"],
        )
        child = Span(
            name="llm",
            trace_id=root.trace_id,
            parent_id=root.span_id,
            start=now + i + 0.1,
            end=now + i + 0.5,
            model="gpt-4o-mini",
            usage={"input_tokens": 10 + i, "output_tokens": 3},
            cost_usd=0.0001 * i,
        )
        store.save_spans([root, child])
        store.save_score("helpful", 1.0 if i % 2 else 0.0, trace_id=root.trace_id)
    store.save_dataset(
        Dataset("drill", [Example(input={"q": f"q{i}"}, expected=f"a{i}") for i in range(5)])
    )
    return hashes


def wipe_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


def media_hashes(root: Path) -> dict[str, str]:
    """{name: sha256 of content} for every media file."""
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()
    }


@pytest.fixture
def seeded(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    db = tmp_path / "live" / "tirkit.db"
    media_dir = tmp_path / "live" / "media"
    st = SQLiteStore(db)
    st.init_schema()
    hashes = seed(st, LocalMediaStore(media_dir))
    st.close()
    return db, media_dir, hashes


def test_sqlite_drill_backup_wipe_restore_compare(seeded, tmp_path: Path) -> None:
    db, media_dir, hashes = seeded
    st = open_store(str(db))
    before = fingerprint(st, sample=None)
    st.close()
    media_before = media_hashes(media_dir)

    out = tmp_path / "backup-1"
    manifest = backup(str(db), out, media_dir=media_dir)
    assert manifest["format"] == "tirkit-backup"
    assert manifest["dialect"] == "sqlite"
    assert manifest["tables"]["spans"]["rows"] == 40
    assert manifest["tables"]["traces"]["rows"] == 20
    assert sorted(m["sha256"] for m in manifest["media"]["files"]) == sorted(hashes)
    assert manifest["media"]["missing"] == []
    assert verify(out)["ok"]

    wipe_sqlite(db)
    shutil.rmtree(media_dir)

    report = restore(out, str(db), media_dir=media_dir)
    assert report["ok"]
    assert report["media_restored"] == 20

    st = open_store(str(db))
    after = fingerprint(st, sample=None)
    # Full-search index came back with the data.
    found = st.query_spans({"search": "walrus"}, limit=100)
    st.close()
    assert after == before
    assert media_hashes(media_dir) == media_before
    assert len(found) == 20


def test_fingerprint_detects_a_changed_row(seeded) -> None:
    db, _, _ = seeded
    st = open_store(str(db))
    a = fingerprint(st, sample=None)
    st.execute(
        "UPDATE spans SET output = '\"changed\"' WHERE name = 'agent' AND output = ?",
        ['"answer 3"'],
    )
    b = fingerprint(st, sample=None)
    st.close()
    assert a["spans"]["rows"] == b["spans"]["rows"]
    assert a["spans"]["sha256"] != b["spans"]["sha256"]
    assert a["traces"] == b["traces"]


def test_backup_is_consistent_while_writes_continue(seeded, tmp_path: Path) -> None:
    db, media_dir, _ = seeded
    writer = open_store(str(db))
    stop = threading.Event()

    def write() -> None:
        i = 0
        while not stop.is_set():
            writer.save_spans([Span(name="busy", start=time.time(), end=time.time())])
            i += 1

    t = threading.Thread(target=write)
    t.start()
    try:
        time.sleep(0.05)
        manifest = backup(str(db), tmp_path / "online", media_dir=media_dir)
    finally:
        stop.set()
        t.join()
        writer.close()
    target = tmp_path / "restored.db"
    report = restore(tmp_path / "online", str(target), media_dir=tmp_path / "m2")
    assert report["ok"]
    st = open_store(str(target))
    assert fingerprint(st)["spans"]["rows"] == manifest["tables"]["spans"]["rows"]
    st.close()


def test_restore_refuses_a_database_with_data_unless_forced(seeded, tmp_path: Path) -> None:
    db, media_dir, _ = seeded
    out = tmp_path / "b"
    backup(str(db), out, media_dir=media_dir)
    with pytest.raises(BackupError, match="not empty"):
        restore(out, str(db), media_dir=media_dir)
    st = open_store(str(db))
    st.save_spans([Span(name="after-backup", start=time.time(), end=time.time())])
    st.close()
    assert restore(out, str(db), media_dir=media_dir, force=True)["ok"]
    st = open_store(str(db))
    assert st.query_spans({"name": "after-backup"}) == []
    st.close()


def test_backup_refuses_a_non_empty_directory(seeded, tmp_path: Path) -> None:
    db, media_dir, _ = seeded
    out = tmp_path / "b"
    out.mkdir()
    (out / "something").write_text("x")
    with pytest.raises(BackupError, match="not empty"):
        backup(str(db), out, media_dir=media_dir)


def test_restore_rejects_corrupt_media(seeded, tmp_path: Path) -> None:
    db, media_dir, hashes = seeded
    out = tmp_path / "b"
    backup(str(db), out, media_dir=media_dir)
    victim = out / "media" / hashes[0][:2] / hashes[0]
    victim.write_bytes(b"tampered")
    assert not verify(out)["ok"]
    with pytest.raises(BackupError, match="media"):
        restore(out, str(tmp_path / "new.db"), media_dir=tmp_path / "new-media")


def test_restore_rejects_a_database_that_does_not_match_the_manifest(
    seeded, tmp_path: Path
) -> None:
    db, media_dir, _ = seeded
    out = tmp_path / "b"
    backup(str(db), out, media_dir=media_dir)
    manifest = json.loads((out / "manifest.json").read_text())
    manifest["tables"]["spans"]["rows"] += 1
    (out / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BackupError, match="spans"):
        restore(out, str(tmp_path / "new.db"), media_dir=tmp_path / "new-media")


def test_restore_refuses_an_incomplete_backup(tmp_path: Path) -> None:
    (tmp_path / "half").mkdir()
    with pytest.raises(BackupError, match="manifest"):
        restore(tmp_path / "half", str(tmp_path / "x.db"))


def test_media_rows_without_files_are_reported(seeded, tmp_path: Path) -> None:
    db, media_dir, _ = seeded
    st = open_store(str(db))
    st.record_media("ab" * 32, "image/png", 10)
    st.close()
    manifest = backup(str(db), tmp_path / "b", media_dir=media_dir)
    assert manifest["media"]["missing"] == ["ab" * 32]


def test_media_files_with_wrong_content_are_not_copied(seeded, tmp_path: Path) -> None:
    db, media_dir, hashes = seeded
    (media_dir / hashes[1][:2] / hashes[1]).write_bytes(b"bit rot")
    manifest = backup(str(db), tmp_path / "b", media_dir=media_dir)
    assert manifest["media"]["corrupt"] == [hashes[1]]
    assert hashes[1] not in {m["sha256"] for m in manifest["media"]["files"]}


def test_backup_without_media(seeded, tmp_path: Path) -> None:
    db, _, _ = seeded
    manifest = backup(str(db), tmp_path / "b")
    assert manifest["media"] is None
    assert restore(tmp_path / "b", str(tmp_path / "r.db"))["ok"]


def test_restore_refuses_a_newer_schema(seeded, tmp_path: Path) -> None:
    db, media_dir, _ = seeded
    out = tmp_path / "b"
    backup(str(db), out, media_dir=media_dir)
    manifest = json.loads((out / "manifest.json").read_text())
    manifest["schema_version"] = 10_000
    (out / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BackupError, match="newer"):
        restore(out, str(tmp_path / "r.db"))


def test_cli_backup_and_restore(seeded, tmp_path: Path) -> None:
    from tirkit.cli import app

    db, media_dir, _ = seeded
    runner = CliRunner()
    out = tmp_path / "cli-backup"
    res = runner.invoke(
        app, ["backup", str(out), "--database-url", str(db), "--media-dir", str(media_dir)]
    )
    assert res.exit_code == 0, res.output
    assert "40 spans" in res.output or "spans" in res.output
    res = runner.invoke(
        app,
        [
            "restore",
            str(out),
            "--database-url",
            str(tmp_path / "r.db"),
            "--media-dir",
            str(tmp_path / "rm"),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "verified" in res.output
    res = runner.invoke(app, ["restore", str(out), "--database-url", str(tmp_path / "r.db")])
    assert res.exit_code == 1
    assert "not empty" in res.output


# --- Postgres ------------------------------------------------------------------------------------

PG_URL = os.environ.get("TIRKIT_TEST_POSTGRES_URL")
PG_DUMP = os.environ.get("TIRKIT_PG_DUMP", "pg_dump")


def _have_pg_tools() -> bool:
    return bool(os.environ.get("TIRKIT_PG_DUMP")) or bool(
        shutil.which("pg_dump") and shutil.which("pg_restore")
    )


@pytest.mark.postgres
@pytest.mark.skipif(not PG_URL, reason="TIRKIT_TEST_POSTGRES_URL not set")
@pytest.mark.skipif(
    not _have_pg_tools(), reason="pg_dump/pg_restore not on PATH and TIRKIT_PG_DUMP not set"
)
def test_postgres_drill_backup_wipe_restore_compare(tmp_path: Path) -> None:
    psycopg = pytest.importorskip("psycopg")

    def wipe() -> None:
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    wipe()
    st = open_store(PG_URL)
    st.init_schema()
    media_dir = tmp_path / "media"
    seed(st, LocalMediaStore(media_dir))
    before = fingerprint(st, sample=None)
    st.close()

    out = tmp_path / "pg-backup"
    manifest = backup(PG_URL, out, media_dir=media_dir)
    assert manifest["dialect"] == "postgres"
    assert (out / "db.dump").stat().st_size > 0
    assert manifest["tables"]["spans"]["rows"] == 40

    with pytest.raises(BackupError, match="not empty"):
        restore(out, PG_URL, media_dir=tmp_path / "m2")

    wipe()
    shutil.rmtree(media_dir)
    report = restore(out, PG_URL, media_dir=media_dir)
    assert report["ok"]
    st = open_store(PG_URL)
    after = fingerprint(st, sample=None)
    found = st.query_spans({"search": "walrus"}, limit=100)
    st.close()
    assert after == before
    assert len(found) == 20


# -- the pg_dump client must not be newer than the server --------------------------------------


def test_pg_client_version_is_read_from_the_version_line() -> None:
    import subprocess

    from tirkit.store import backup as b

    for line, major in [
        ("pg_dump (PostgreSQL) 16.4 (Debian 16.4-1.pgdg120+1)", 16),
        ("pg_dump (PostgreSQL) 17.2", 17),
        ("pg_dump (PostgreSQL) 9.6.24", 9),
    ]:
        done = subprocess.CompletedProcess([], 0, stdout=line.encode(), stderr=b"")
        import pytest

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(b.subprocess, "run", lambda *a, d=done, **k: d)
            assert b._client_major(["pg_dump"], {}) == major
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(b.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        assert b._client_major(["pg_dump"], {}) is None


def test_a_newer_pg_dump_than_the_server_is_refused_with_a_clear_message() -> None:
    import pytest

    from tirkit.store.backup import BackupError, _check_client_version

    with pytest.raises(BackupError, match="version 17.*server is version 16"):
        _check_client_version(17, 160004)


@pytest.mark.parametrize(
    "client,server", [(16, 160004), (15, 160004), (None, 160004), (14, 140012)]
)
def test_same_or_older_or_unknown_pg_dump_is_accepted(client: int | None, server: int) -> None:
    from tirkit.store.backup import _check_client_version

    _check_client_version(client, server)
