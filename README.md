# IRIS-CAND-22: One-Command Pilot Orchestration

One command promotes accepted data, refreshes the BESS and peatland candidate views, exports
sample dossiers for both verticals, and writes a run manifest. Failed and stale stages are
always visible, and a failed critical stage can never be reported as success.

**Stack:** Python 3.12+, PostgreSQL 16, PostGIS 3.4 (via Docker), `psycopg` 3, `pytest`.
No orchestration framework: a small stage runner is all the pilot needs.

## Quick start

Prerequisites: Docker Desktop (running) and Python 3.12+.

```bash
docker compose up -d --wait          # PostgreSQL 16 + PostGIS 3.4 on localhost:5433
pip install -e ".[test]"             # ideally inside a virtual env / conda env

python -m iris run                   # THE one command
python -m iris status                # last run + live staleness check
python -m pytest -v                  # 12 tests
```

Exit codes: `0` means success; `1` means failed or something is stale.

Reset to an empty database at any time: `docker compose down -v && docker compose up -d --wait`.

### Output

```
out/
  dossiers/bess/DE_DE-BESS-001.md ...      + index.json (run id, export time, source view refresh)
  dossiers/peatland/DE_DE-PEAT-001.md ...  + index.json
  runs/<run_id>.json                       run manifest (one per run)
  runs/latest.json                         copy of the most recent manifest
```

Configuration: `--dsn` or env `IRIS_DSN` (default `postgresql://iris:iris@localhost:5433/iris`),
`--out` (default `out`), `--fixtures` (default `fixtures`), `--limit` (sample dossiers per vertical, default 5).
Tests create and drop their own throwaway databases via `IRIS_TEST_DSN`
(default `postgresql://iris:iris@localhost:5433/postgres`), so they never touch the working database.

## Architecture

```
fixtures/*.csv ─ingest─► staging ─promote─► core ─refresh─► mart (materialized views) ─export─► dossiers
                         (as delivered)     (accepted + valid)  (screening + reasons)          (.md)
                                       ops: migrations, change tracking, pipeline state
```

| Path | Responsibility |
|---|---|
| `migrations/001_core.sql` | Schemas, staging and core tables, change-tracking triggers |
| `migrations/002_candidate_views.sql` | `mart.bess_candidates`, `mart.peatland_candidates` |
| `iris/runner.py` | Generic ordered stage runner + run manifest (no DB knowledge) |
| `iris/stages.py` | The 8 IRIS stages + freshness check |
| `iris/dossier.py` | Dossier rendering (row in, Markdown out) |
| `iris/cli.py` | `run` and `status` commands |

### Stages

| # | Stage | Critical | Depends on | What it does |
|---|---|---|---|---|
| 1 | `preflight` | yes | | Fixtures, migrations, output dir, DB connection, PG >= 16, PostGIS >= 3.4, run lock |
| 2 | `migrate` | yes | 1 | Applies new `migrations/*.sql` in order, each in its own transaction, checksum-guarded |
| 3 | `ingest_fixtures` | yes | 2 | Stand-in for source adapters: upserts CSV fixtures into staging / reference tables |
| 4 | `promote_accepted` | yes | 3 | Upserts `accepted` rows that pass validation into `core`; reports rejected rows with reasons |
| 5 | `refresh_bess_view` | no | 4 | Refreshes the BESS view and records its input watermark |
| 6 | `refresh_peatland_view` | no | 4 | Same for peatland |
| 7 | `export_bess_dossiers` | no | 5 | Writes sample dossiers via a temp directory + atomic swap |
| 8 | `export_peatland_dossiers` | no | 6 | Same for peatland |

After the stages, a **freshness check** compares every view and export against its inputs.

### Failure semantics

- A stage runs only if all its dependencies succeeded; otherwise it is `skipped` with a recorded reason.
- A failed **critical** stage aborts the run: every later stage is skipped.
- Per-vertical stages are non-critical, so a peatland failure does not hide BESS output, **but the run is still `failed`**.
- The run is `success` **only** if every stage succeeded **and** nothing is stale. There is no "partially OK" status.
- The manifest is written with `status: running` before the first stage and after every stage
  (atomic file replace). A crash or Ctrl+C leaves `running`/`failed`, never `success`.

### Staleness

- Row-level triggers on every `core` table record when it last changed (`ops.source_changes`),
  which also catches manual edits that bypass the pipeline.
- Each refresh stores the input watermark it was built from (`ops.pipeline_state`), in the same
  transaction as the refresh.
- A **view is stale** if its inputs changed after its last refresh, or it is missing / never refreshed.
- An **export is stale** if its view was refreshed after it, the view is stale, or its files are missing.
- `python -m iris status` runs the check live at any time, not only during a run.

### Idempotency (rerun safety)

- Migrations are recorded in `ops.schema_migrations`; editing an applied migration fails loudly.
- All writes are upserts with an `IS DISTINCT FROM` guard: unchanged rows are not touched, so a rerun
  reports `inserted 0 / updated 0 / unchanged N` and does not bump change tracking.
- Exports are built in a temp directory and swapped in; a failed export leaves the previous complete set.
  Dossier content contains no timestamps, so unchanged data gives byte-identical files.
- A PostgreSQL advisory lock prevents two concurrent runs.

## Data contracts and assumptions

- Geometry column `geom`, CRS **EPSG:25832** (ETRS89 / UTM 32N, metres), enforced by the column type in `core`.
- Every table has a non-null `country_code`; keys are `(country_code, id)`; all spatial and
  assumption joins are country-scoped. The 8 eco-points/m2 factor is configured for DE only, so the
  AT fixture shows eco-points as "not available" instead of borrowing the German factor.
- **Nothing is invented.** Accepted rows with missing geometry, wrong or unknown CRS, non-polygon or
  invalid geometry are rejected with a stated reason. Missing attributes stay NULL and are shown as
  "not available" / "unscreened".
- Source name, source date and uncertainty are carried from staging into every dossier.
- Every dossier ends with the project's reference uncertainty wording.
- **Screening thresholds are pilot assumptions:** BESS band A = substation <= 2 km and parcel >= 2 ha,
  B = <= 5 km, C = further; peatland A = depth >= 1.0 m and area >= 5 ha, B = depth >= 0.3 m, C = thinner.

## Fixtures

`fixtures/` holds a small synthetic, deterministic dataset (all names marked "Synthetic"):
11 staging submissions, 3 substations, 1 commercial assumption. It deliberately includes a pending row,
a row rejected in review, a missing geometry, a row in EPSG:4326, a self-intersecting polygon, and a
second country (AT) without an eco-point factor.
Expected result: 3 BESS + 3 peatland dossiers, 3 rows rejected with warnings.

## Tests

- `tests/test_runner.py` (no DB): critical abort, dependency skip, stale output blocks success,
  interrupted run recorded as failed.
- `tests/test_pipeline.py` (real PostGIS, throwaway DB per test): both verticals from one command;
  invalid rows rejected not repaired; rerun idempotent and byte-identical; **failing refresh stage
  makes the run failed, skips its export and flags it stale**; failing critical preflight aborts
  everything; a failed export keeps the previous dossiers; a manual edit makes view and export stale.

## Deliberate simplifications and the production path

| Pilot | Production |
|---|---|
| CSV fixtures loaded by `ingest_fixtures` | Country-aware source adapters writing into staging |
| Rows in a non-contract CRS are rejected | Transform from the declared source CRS with `ST_Transform`, keeping the original |
| `review_status` set in the data | Review workflow with reviewer, timestamp and audit trail |
| Promotion only upserts | Handle rows withdrawn after acceptance (soft delete + staleness) |
| Plain `REFRESH` (atomic with its state row, briefly blocks readers) | `REFRESH ... CONCURRENTLY` (unique indexes already exist) |
| Markdown dossiers, top N per vertical | PDF rendering from the same data, with maps |
| Manifest as JSON files | Also persist to an `ops.runs` table and alert on failed / stale |
| Screening thresholds in SQL | Versioned, country-scoped screening parameters table |
| Single process + advisory lock | Same design from cron / CI; a scheduler only once there are many independent pipelines |