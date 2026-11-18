"""Backup and restore of a whole Tirkit installation: database plus media files.

A backup is a directory:

- ``db.sqlite`` (SQLite, taken with the online backup API, so writers can keep going) or
  ``db.dump`` (Postgres, ``pg_dump`` custom format, taken inside an exported snapshot);
- ``media/<sha[:2]>/<sha>``: every media file whose content matches its name;
- ``manifest.json``, written last, so a directory without it is an incomplete backup. It records
  the schema version, a fingerprint of every table (row count plus the SHA-256 of the first
  ``sample`` rows in primary-key order) and the media files by hash.

``restore`` checks the media hashes before it touches the database, restores, then fingerprints
the restored database and compares it with the manifest. It refuses a target that already holds
data unless ``force=True``.

``pg_dump`` and ``pg_restore`` must be on ``PATH`` (the same major version as the server) or be
named by ``$TIRKIT_PG_DUMP`` / ``$TIRKIT_PG_RESTORE``; those may be whole commands, such as
``docker exec -i -e PGPASSWORD db pg_dump``. The dump is streamed through stdout and stdin, and the
password is passed in ``$PGPASSWORD`` rather than on the command line.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path
from typing import Any

FORMAT = "tirkit-backup"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
SQLITE_FILE = "db.sqlite"
PG_FILE = "db.dump"
MEDIA = "media"
DEFAULT_SAMPLE = 100
_CHUNK = 1 << 20
_SHA = re.compile(r"[0-9a-f]{64}")


class BackupError(RuntimeError):
    """A backup or restore that cannot go ahead, or whose result does not verify."""


# --- Fingerprints ---


def fingerprint(store: Any, sample: int | None = DEFAULT_SAMPLE) -> dict[str, dict[str, Any]]:
    """Row count and content hash per table: ``{table: {rows, sampled, sha256}}``.

    The hash covers the first ``sample`` rows in primary-key order (``None``: every row), so it
    uses the primary-key index and stays cheap on large tables.
    """
    with store.tx() as cur:
        return _fingerprint(cur, store.dialect.name, sample)


def _fingerprint(cur: Any, dialect: str, sample: int | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for table in _tables(cur, dialect):
        q = _quote(table)
        cur.execute(f"SELECT COUNT(*) FROM {q}")
        rows = int(cur.fetchone()[0])
        order = ", ".join(_order_by(cur, dialect, table))
        sql = f"SELECT * FROM {q} ORDER BY {order}"
        if sample is not None:
            sql += f" LIMIT {int(sample)}"
        h = hashlib.sha256()
        n = 0
        for row in _stream(cur, dialect, sql):
            h.update(json.dumps([_norm(v) for v in row], sort_keys=True, default=str).encode())
            h.update(b"\n")
            n += 1
        out[table] = {"rows": rows, "sampled": n, "sha256": h.hexdigest()}
    return out


def _stream(cur: Any, dialect: str, sql: str) -> Iterator[tuple[Any, ...]]:
    """Rows without loading the whole result: a server-side cursor on Postgres."""
    conn = cur.connection
    if dialect == "postgres":
        with conn.cursor(name="tirkit_fingerprint") as named:
            named.itersize = 2000
            named.execute(sql)
            yield from named
    else:
        other = conn.cursor()
        try:
            yield from other.execute(sql)
        finally:
            other.close()


def _tables(cur: Any, dialect: str) -> list[str]:
    if dialect == "postgres":
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename"
        )
        return [r[0] for r in cur.fetchall()]
    cur.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name")
    rows = cur.fetchall()
    virtual = [n for n, sql in rows if (sql or "").upper().startswith("CREATE VIRTUAL TABLE")]
    # Full-text indexes (and their shadow tables) are rebuilt from the data; skip them.
    return [
        n
        for n, _ in rows
        if not n.startswith("sqlite_")
        and n not in virtual
        and not any(n.startswith(v + "_") for v in virtual)
    ]


def _order_by(cur: Any, dialect: str, table: str) -> list[str]:
    """Primary-key columns, or every column as text for tables without a primary key."""
    if dialect == "postgres":
        cur.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary "
            "ORDER BY array_position(i.indkey::int2[], a.attnum)",
            (_quote(table),),
        )
        pk = [r[0] for r in cur.fetchall()]
        if not pk:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = "
                "current_schema() AND table_name = %s ORDER BY ordinal_position",
                (table,),
            )
            return [f"CAST({_quote(r[0])} AS TEXT)" for r in cur.fetchall()]
    else:
        cur.execute(f"PRAGMA table_info({_quote(table)})")
        info = cur.fetchall()
        pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5] > 0]
        if not pk:
            return [f"CAST({_quote(r[1])} AS TEXT)" for r in info]
    return [_quote(c) for c in pk]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _norm(v: Any) -> Any:
    if isinstance(v, bytes | bytearray | memoryview):
        return bytes(v).hex()
    if isinstance(v, float):
        return repr(v)
    return v


def _schema_version(cur: Any) -> int:
    cur.execute("SELECT MAX(version) FROM schema_migrations")
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _media_hashes(cur: Any) -> set[str]:
    cur.execute("SELECT DISTINCT sha256 FROM media")
    return {r[0] for r in cur.fetchall()}


def _has_data(cur: Any, dialect: str) -> bool:
    """Any row in any table other than ``schema_migrations`` (a fresh schema counts as empty)."""
    for table in _tables(cur, dialect):
        if table == "schema_migrations":
            continue
        cur.execute(f"SELECT 1 FROM {_quote(table)} LIMIT 1")
        if cur.fetchone():
            return True
    return False


# --- Media ---


def _copy_verified(src: Path, dest: Path, sha256: str) -> bool:
    """Copy ``src`` to ``dest`` through a temporary file, hashing as it goes. False on mismatch."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    h = hashlib.sha256()
    with src.open("rb") as fin, tmp.open("wb") as fout:
        while chunk := fin.read(_CHUNK):
            h.update(chunk)
            fout.write(chunk)
    if h.hexdigest() != sha256:
        tmp.unlink()
        return False
    tmp.replace(dest)
    return True


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _media_files(root: Path) -> Iterator[Path]:
    """Files laid out as ``<root>/<sha[:2]>/<sha>``; temporary and stray files are skipped."""
    if not root.is_dir():
        return
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or len(sub.name) != 2:
            continue
        for p in sorted(sub.iterdir()):
            if p.is_file() and _SHA.fullmatch(p.name) and p.name[:2] == sub.name:
                yield p


def _backup_media(media_dir: Path, out: Path, known: set[str]) -> dict[str, Any]:
    files, corrupt = [], []
    for p in _media_files(media_dir):
        if _copy_verified(p, out / MEDIA / p.name[:2] / p.name, p.name):
            files.append({"sha256": p.name, "size": p.stat().st_size})
        else:
            corrupt.append(p.name)
    present = {f["sha256"] for f in files}
    return {
        "files": files,
        "bytes": sum(f["size"] for f in files),
        "corrupt": corrupt,
        "missing": sorted(known - present - set(corrupt)),
    }


def _restore_media(
    backup_dir: Path, entries: list[dict[str, Any]], media_dir: Path
) -> dict[str, int]:
    """Copy media into ``media_dir`` (content-addressed, so never destructive). Raises if any
    file in the backup fails its hash; the database has not been touched at that point."""
    restored = present = 0
    bad: list[str] = []
    for e in entries:
        sha = e["sha256"]
        if not _SHA.fullmatch(sha):
            bad.append(sha)
            continue
        dest = media_dir / sha[:2] / sha
        if dest.is_file() and _file_sha(dest) == sha:
            present += 1
            continue
        src = backup_dir / MEDIA / sha[:2] / sha
        if not src.is_file() or not _copy_verified(src, dest, sha):
            bad.append(sha)
            continue
        restored += 1
    if bad:
        raise BackupError(
            f"{len(bad)} media file(s) in the backup are missing or fail their hash, "
            f"e.g. {bad[0]}; the database was not restored"
        )
    return {"media_restored": restored, "media_present": present}


# --- SQLite ---


def _sqlite_path(url: str) -> Path:
    return Path(url[len("sqlite:///") :] if url.startswith("sqlite:///") else url)


def _backup_sqlite(path: Path, out: Path, sample: int | None) -> dict[str, Any]:
    if not path.is_file():
        raise BackupError(f"no SQLite database at {path}")
    dest = out / SQLITE_FILE
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)  # one step: a consistent snapshot even while others write
        dst.execute("PRAGMA journal_mode=DELETE")  # one self-contained file
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BackupError("the SQLite copy failed its integrity check")
        cur = dst.cursor()
        info = {
            "schema_version": _schema_version(cur),
            "tables": _fingerprint(cur, "sqlite", sample),
            "known_media": _media_hashes(cur),
        }
    finally:
        dst.close()
        src.close()
    info["db_file"] = SQLITE_FILE
    info["db_bytes"] = dest.stat().st_size
    return info


def _restore_sqlite(backup_dir: Path, path: Path, force: bool) -> None:
    src_path = backup_dir / SQLITE_FILE
    if not src_path.is_file():
        raise BackupError(f"{src_path} is missing")
    path.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(str(path))
    try:
        if not force and _has_data(dst.cursor(), "sqlite"):
            raise BackupError(f"target database {path} is not empty (use --force to replace it)")
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        try:
            src.backup(dst)
        finally:
            src.close()
        dst.execute("PRAGMA journal_mode=WAL")
    finally:
        dst.close()


def _sqlite_fingerprint(path: Path, sample: int | None) -> dict[str, dict[str, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return _fingerprint(conn.cursor(), "sqlite", sample)
    finally:
        conn.close()


# --- Postgres ---


def _is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


def _pg_command(env_var: str, default: str) -> list[str]:
    return shlex.split(os.environ.get(env_var) or default)


def _pg_target(url: str) -> tuple[str, dict[str, str]]:
    """A conninfo string without the password, and an environment carrying it."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(url)
    password = params.pop("password", None)
    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = str(password)
    return make_conninfo(**{k: str(v) for k, v in params.items()}), env


def _run_pg(cmd: list[str], env: dict[str, str], **kw: Any) -> None:
    try:
        proc = subprocess.run(cmd, env=env, stderr=subprocess.PIPE, check=False, **kw)
    except FileNotFoundError as exc:
        raise BackupError(
            f"{cmd[0]} not found: install the Postgres client tools (same major version as the "
            "server) or set $TIRKIT_PG_DUMP / $TIRKIT_PG_RESTORE"
        ) from exc
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise BackupError(f"{cmd[0]} failed ({proc.returncode}): {err[-2000:]}")


_PG_VERSION = re.compile(r"\(PostgreSQL\)\s+(\d+)")


def _client_major(cmd: list[str], env: dict[str, str]) -> int | None:
    """Major version of the ``pg_dump`` that will run, or None when it cannot be read."""
    try:
        done = subprocess.run(
            [*cmd, "--version"], env=env, capture_output=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = _PG_VERSION.search(done.stdout.decode(errors="replace"))
    return int(found.group(1)) if found else None


def _check_client_version(client_major: int | None, server_version_num: int) -> None:
    """Refuse a ``pg_dump`` newer than the server: its dump can contain settings an older server
    does not know, so the backup would be written but could not be restored."""
    server_major = server_version_num // 10000
    if client_major is not None and client_major > server_major:
        raise BackupError(
            f"pg_dump is version {client_major} but the server is version {server_major}: a dump "
            "from a newer client may not restore into this server. Install the Postgres "
            f"{server_major} client tools, or point $TIRKIT_PG_DUMP at them"
        )


def _backup_postgres(url: str, out: Path, sample: int | None) -> dict[str, Any]:
    import psycopg

    dsn, env = _pg_target(url)
    dest = out / PG_FILE
    with psycopg.connect(url) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("SHOW server_version_num")
            server_version_num = int(cur.fetchone()[0])  # type: ignore[index]
            cur.execute("SELECT pg_export_snapshot()")
            snapshot = cur.fetchone()[0]  # type: ignore[index]
            # The fingerprint and pg_dump see the same snapshot, so they agree even under load.
            info: dict[str, Any] = {
                "schema_version": _schema_version(cur),
                "tables": _fingerprint(cur, "postgres", sample),
                "known_media": _media_hashes(cur),
            }
        dump = _pg_command("TIRKIT_PG_DUMP", "pg_dump")
        _check_client_version(_client_major(dump, env), server_version_num)
        cmd = dump + [
            "--format=custom",
            f"--snapshot={snapshot}",
            f"--dbname={dsn}",
        ]
        with dest.open("wb") as f:
            _run_pg(cmd, env, stdout=f)
        conn.rollback()
    if dest.stat().st_size == 0:
        raise BackupError("pg_dump wrote nothing")
    info["db_file"] = PG_FILE
    info["db_bytes"] = dest.stat().st_size
    return info


def _restore_postgres(backup_dir: Path, url: str, force: bool) -> None:
    import psycopg

    src = backup_dir / PG_FILE
    if not src.is_file():
        raise BackupError(f"{src} is missing")
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        if not force and _has_data(cur, "postgres"):
            raise BackupError("target database is not empty (use --force to replace it)")
    dsn, env = _pg_target(url)
    cmd = _pg_command("TIRKIT_PG_RESTORE", "pg_restore") + [
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--single-transaction",
        "--exit-on-error",
        f"--dbname={dsn}",
    ]
    with src.open("rb") as f:
        _run_pg(cmd, env, stdin=f)


def _pg_fingerprint(url: str, sample: int | None) -> dict[str, dict[str, Any]]:
    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        return _fingerprint(cur, "postgres", sample)


# --- Public API ---


def backup(
    database_url: str,
    out_dir: str | Path,
    media_dir: str | Path | None = None,
    *,
    sample: int | None = DEFAULT_SAMPLE,
) -> dict[str, Any]:
    """Back up the database (and media files, if ``media_dir`` is given) into ``out_dir``.

    ``out_dir`` must be new or empty. Returns the manifest.
    """
    started = time.time()
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise BackupError(f"backup directory {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    if _is_postgres(database_url):
        dialect = "postgres"
        info = _backup_postgres(database_url, out, sample)
    else:
        dialect = "sqlite"
        info = _backup_sqlite(_sqlite_path(database_url), out, sample)
    known = info.pop("known_media")
    media = _backup_media(Path(media_dir), out, known) if media_dir is not None else None
    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "tirkit_version": _version(),
        "created": started,
        "duration_s": round(time.time() - started, 3),
        "dialect": dialect,
        "sample": sample,
        **info,
        "media": media,
    }
    tmp = out / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1))
    tmp.replace(out / MANIFEST)  # written last: its presence marks a complete backup
    return manifest


def read_manifest(backup_dir: str | Path) -> dict[str, Any]:
    path = Path(backup_dir) / MANIFEST
    if not path.is_file():
        raise BackupError(f"{path} not found: not a backup, or an incomplete one")
    manifest = json.loads(path.read_text())
    if manifest.get("format") != FORMAT:
        raise BackupError(f"{path} is not a Tirkit backup manifest")
    if int(manifest.get("format_version", 0)) > FORMAT_VERSION:
        raise BackupError("backup format is newer than this version of Tirkit")
    return manifest


def _compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    problems = []
    for table, want in expected.items():
        got = actual.get(table)
        if got is None:
            problems.append(f"{table}: missing")
        elif got["rows"] != want["rows"]:
            problems.append(f"{table}: {got['rows']} rows, expected {want['rows']}")
        elif got["sha256"] != want["sha256"]:
            problems.append(f"{table}: content hash differs")
    return problems


def restore(
    backup_dir: str | Path,
    database_url: str,
    media_dir: str | Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Restore a backup into ``database_url`` (and ``media_dir``), then verify it.

    Order: read the manifest, check media hashes while copying (nothing is lost if this fails:
    media is content-addressed), restore the database, fingerprint it and compare with the
    manifest. Raises :class:`BackupError` on any problem.
    """
    from tirkit.store.migrations import MIGRATIONS

    src = Path(backup_dir)
    manifest = read_manifest(src)
    if int(manifest.get("schema_version") or 0) > len(MIGRATIONS):
        raise BackupError(
            f"backup schema version {manifest['schema_version']} is newer than this Tirkit "
            f"({len(MIGRATIONS)}); upgrade Tirkit before restoring"
        )
    target = "postgres" if _is_postgres(database_url) else "sqlite"
    if manifest["dialect"] != target:
        raise BackupError(f"backup is from {manifest['dialect']}, target is {target}")
    sample = manifest.get("sample")
    expected = manifest["tables"]
    if target == "sqlite":
        problems = _compare(expected, _sqlite_fingerprint(src / SQLITE_FILE, sample))
        if problems:
            raise BackupError("backup database does not match its manifest: " + "; ".join(problems))
    media_report = {"media_restored": 0, "media_present": 0}
    if manifest.get("media") and media_dir is not None:
        media_report = _restore_media(src, manifest["media"]["files"], Path(media_dir))
    if target == "sqlite":
        path = _sqlite_path(database_url)
        _restore_sqlite(src, path, force)
        actual = _sqlite_fingerprint(path, sample)
    else:
        _restore_postgres(src, database_url, force)
        actual = _pg_fingerprint(database_url, sample)
    problems = _compare(expected, actual)
    if problems:
        raise BackupError("restored database does not match the manifest: " + "; ".join(problems))
    return {
        "ok": True,
        "dialect": target,
        "tables": len(expected),
        "rows": sum(t["rows"] for t in expected.values()),
        **media_report,
    }


def verify(backup_dir: str | Path) -> dict[str, Any]:
    """Check a backup without restoring it: manifest, database file and every media hash."""
    src = Path(backup_dir)
    problems: list[str] = []
    try:
        manifest = read_manifest(src)
    except BackupError as exc:
        return {"ok": False, "problems": [str(exc)]}
    db_file = src / manifest.get("db_file", "")
    if not db_file.is_file() or db_file.stat().st_size == 0:
        problems.append(f"{db_file.name}: missing or empty")
    elif manifest["dialect"] == "sqlite":
        actual = _sqlite_fingerprint(db_file, manifest.get("sample"))
        problems += _compare(manifest["tables"], actual)
    for e in (manifest.get("media") or {}).get("files", []):
        p = src / MEDIA / e["sha256"][:2] / e["sha256"]
        if not p.is_file() or _file_sha(p) != e["sha256"]:
            problems.append(f"media {e['sha256']}: missing or fails its hash")
    return {"ok": not problems, "problems": problems}


def _version() -> str:
    try:
        return metadata.version("tirkit")
    except metadata.PackageNotFoundError:
        return "unknown"
