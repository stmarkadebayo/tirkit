# Operations

Backups and the restore drill, monitoring, upgrades, retention, Kubernetes and sizing. Every
number on this page was measured; each says how. Where nothing has been measured, the page says
so instead of guessing.

## Backup and restore

`tirkit backup` writes a directory with the database, the media files and a `manifest.json`:

```bash
tirkit backup /backups/tirkit-2026-11-17 \
  --database-url "$TIRKIT_DATABASE_URL" --media-dir /data/media
```

| Database | How it is copied | Safe while the server runs? |
| --- | --- | --- |
| SQLite | SQLite's online backup API, in one step, into `db.sqlite` | Yes: writers carry on and the copy is a consistent snapshot |
| Postgres | `pg_dump --format=custom` into `db.dump`, inside an exported snapshot | Yes: the dump and the manifest's row counts come from the same snapshot |

The manifest records the schema version, each table's row count and a SHA-256 over its first 100
rows in primary-key order (`--sample` changes the number), and every media file by its SHA-256.
It is written last, so a directory without `manifest.json` is an incomplete backup. Media files
whose content does not match their name are skipped and listed as `corrupt`; media rows with no
file are listed as `missing`. The command prints both as warnings.

Postgres backups need `pg_dump` and `pg_restore` from the same major version as the server (16 for
the bundled Compose and Helm Postgres). If they are not on `PATH`, name a command instead; the dump
is streamed through stdout and stdin, and the password is passed in `PGPASSWORD`, never on the
command line:

```bash
export TIRKIT_PG_DUMP="docker exec -i -e PGPASSWORD tirkit-db-1 pg_dump"
export TIRKIT_PG_RESTORE="docker exec -i -e PGPASSWORD tirkit-db-1 pg_restore"
```

The server image does not include the Postgres client tools; run backups from a machine or
container that has them.

To restore, stop the server and the worker, then:

```bash
tirkit restore /backups/tirkit-2026-11-17 \
  --database-url "$TIRKIT_DATABASE_URL" --media-dir /data/media
```

Restore checks every media file's hash while copying it, before it touches the database (media is
stored by hash, so copying is never destructive). It then restores the database (Postgres:
`pg_restore --clean --if-exists --single-transaction --no-owner`), fingerprints every table and
compares the result with the manifest. Any difference is an error. It refuses a database that
already holds data unless you pass `--force`; a database with only an empty schema counts as empty.
It refuses a backup taken by a newer version of Tirkit. Start the server afterwards: it applies any
newer migrations.

`tirkit backup` does not rotate or upload backups. Copy the directory off the machine (for example
`tar` it and send it to object storage) and delete old ones with your usual tools.

### Restore drill

A backup you have never restored is a hope. `scripts/restore_drill.py` runs `tirkit backup`, wipes
the database and media directory, runs `tirkit restore`, then compares a full fingerprint of every
table and re-hashes a sample of media files. It deletes the database it is given, so point it at a
scratch copy, never at production:

```bash
python scripts/restore_drill.py --yes --seed 2000 \
  --database-url /tmp/drill.db --media-dir /tmp/drill-media
python scripts/restore_drill.py --yes --seed 2000 \
  --database-url postgresql://tirkit:tirkit@localhost:5432/scratch --media-dir /tmp/pg-media
```

CI runs both every week and on every change to the store, the CLI or the chart
(`.github/workflows/ops.yml`). To drill a real installation: restore last night's backup into a
scratch database with `tirkit restore` (which verifies it), then run the drill there without
`--seed`.

## Monitoring

`GET /metrics` serves Prometheus metrics on the server port. It is not part of the JSON API.

| Metric | Type | What it shows |
| --- | --- | --- |
| `tirkit_ingest_requests_total{route, outcome, reason}` | counter | Ingest requests accepted or rejected. `route`: `spans`, `otlp`, `media`, `scores`. `reason`: `ok`, `invalid`, `auth`, `too_large`, `rate_limited`, `not_found`, `error`, `other` |
| `tirkit_ingest_spans_total{route}` | counter | Spans accepted |
| `tirkit_ingest_request_duration_seconds{route}` | histogram | Ingest latency, 1 ms to 10 s buckets |
| `tirkit_ingest_buffer_depth` | gauge | Spans waiting in the server's ingest buffer (0 while ingest writes directly) |
| `tirkit_db_pool_max`, `tirkit_db_pool_connections{state}`, `tirkit_db_pool_waiting` | gauges | Postgres connection pool; saturation is `in_use / max` |
| `tirkit_jobs{state}` | gauge | Queued and running background jobs |
| `tirkit_jobs_oldest_ready_age_seconds` | gauge | How long the oldest runnable job has waited |
| `tirkit_judge_spend_today_usd`, `tirkit_judge_spend_usd_total` | gauge, counter | LLM-judge spend, all projects |
| `tirkit_db_up` | gauge | 1 if the database answered the scrape |

Labels never carry trace, span, project or user identifiers, so the number of series stays fixed.
Queue and spend figures are read from the database at scrape time only. With several server
processes (`tirkit serve --workers N`), counters are shared through files in
`$PROMETHEUS_MULTIPROC_DIR`, which `tirkit serve` creates when it is not set, so every scrape sees
the sum over processes. Set `TIRKIT_METRICS_TOKEN` to require `Authorization: Bearer <token>`, or
`TIRKIT_METRICS=0` to turn the endpoint off.

What the metrics cost on the request path, measured with `research/benchmarks/bench_metrics.py`
(an empty ASGI app called directly, 200,000 requests, 3 runs, Apple M3 Pro, Python 3.12): about
2.0 µs added per ingest request (3.1 µs with multi-process counters) and 0.4 µs per other request.

Useful alerts: `rate(tirkit_ingest_requests_total{outcome="rejected"}[5m])` rising,
`tirkit_jobs_oldest_ready_age_seconds > 600`, `tirkit_db_pool_waiting > 0` for several minutes,
`tirkit_db_up == 0`.

## Upgrades

1. Take a backup (above) and check that `tirkit backup` printed no warnings.
2. Read the release notes in `CHANGELOG.md`.
3. Deploy the new version. Migrations run when the server or worker starts; on Postgres they hold
   an advisory lock, so several replicas starting together are safe.
4. Watch `/api/health` and `/metrics`.

Downgrades are not supported once a newer version has migrated the database: restore the backup
taken before the upgrade instead. `tirkit restore` refuses a backup whose schema is newer than the
installed Tirkit.

## Retention

Each project has a retention period in **Settings** (`retention_days`). The worker checks every
hour and deletes spans, traces and trace scores that started before the cutoff. Experiments,
datasets and their scores are kept.

Not yet covered by retention: media files and their rows, finished rows in the `jobs` table, and
the daily `judge_spend` rows. They grow until removed by hand. On Postgres, deleted rows free
space for reuse after autovacuum; the files shrink only after `VACUUM FULL`.

## Kubernetes (Helm)

`deploy/helm/tirkit` deploys the server and the worker, with an optional single-instance Postgres.

```bash
helm install tirkit deploy/helm/tirkit \
  --set image.repository=ghcr.io/stmarkadebayo/tirkit --set image.tag=v0.1.0
```

The image is published to `ghcr.io/stmarkadebayo/tirkit` with each release; use your own registry
by building the repository's `Dockerfile` and pushing it there.

- **Server**: a Deployment (2 replicas by default, optional HPA on CPU), startup, readiness and
  liveness probes on `/api/health`, and a PodDisruptionBudget when more than one replica runs.
  `/api/health` also checks the database, so the liveness probe allows two minutes of failures
  before restarting a pod.
- **Worker**: a Deployment running `tirkit worker`. It serves no HTTP, so it has no probes.
- **Postgres** (`postgresql.enabled`, on by default): one StatefulSet pod for trials and small
  installs. Its password is generated on install and kept on upgrade. For production, use a managed
  Postgres: `--set postgresql.enabled=false --set secrets.databaseUrl=postgresql://...`.
- **Secrets**: the chart creates one Secret, or uses `secrets.existingSecret`, which must hold
  `DATABASE_URL` (plus `POSTGRES_PASSWORD` with the bundled Postgres) and may hold
  `ADMIN_PASSWORD`, `LLM_API_KEY`, `ANTHROPIC_API_KEY` and `METRICS_TOKEN`.
- **Media**: one PersistentVolumeClaim shared by every server and worker pod, so it must be
  `ReadWriteMany` (NFS, EFS, Filestore, Azure Files, CephFS) when more than one pod runs.
- **Security**: pods run as non-root (uid 10001; Postgres as 70) with a read-only root filesystem,
  no privilege escalation, all capabilities dropped, the default seccomp profile and no service
  account token.
- **Metrics**: server pods carry `prometheus.io/scrape`, `port` and `path` annotations.

CI runs `helm lint --strict` and `kubeconform -strict` against Kubernetes 1.29 and 1.31 on three
value sets (`deploy/helm/ci/`).

## Sizing

Measured so far (all on one Apple M3 Pro laptop; see README Results for the first three):

| What | Measured | How |
| --- | --- | --- |
| Ingestion throughput | 1,679–3,105 spans/s, nothing dropped | `research/benchmarks/bench_ingest.py`, 8 clients, `tirkit serve` with 2 workers, Postgres 16 in Docker, 3 runs |
| Trace list query | 47 ms p95 (worst filter 83 ms) at 1,000,000 spans | `research/benchmarks/bench_query.py`, Postgres 16 in Docker |
| Tracing overhead in your app | 0.024–0.025 ms p99 per traced call | `research/benchmarks/bench_tracing.py` |
| SQLite size and backup | 300,000 spans (100,000 traces of 3 spans, ~435,000 rows in all) plus 10,000 media files (11 MB): database 207 MB; backup directory 218 MB; `tirkit backup` 3.9–4.8 s; `tirkit restore` 3.2–3.3 s including verification | `scripts/restore_drill.py --seed 100000`, then twice more on the same data: 3 runs, on the laptop's SSD. Backup time is the manifest's `duration_s`; restore time is the whole command |
| Postgres size and backup | The same data: database 243 MB after restore (`pg_database_size`, indexes included); `pg_dump` custom-format file 17 MB, backup directory 29 MB with media; `tirkit backup` 4.5–5.4 s; `tirkit restore` 9.6–9.7 s including verification | Same drill, 3 runs, Postgres 16 in Docker on the same laptop, `pg_dump`/`pg_restore` 16 run inside the Postgres container through `TIRKIT_PG_DUMP`/`TIRKIT_PG_RESTORE` |

Not measured yet: memory and CPU per server or worker process under load, and backup times beyond
300,000 spans or over a network. The Helm chart's resource requests are
starting points, not measurements; check your own usage with `/metrics` and `kubectl top` before
relying on them.
