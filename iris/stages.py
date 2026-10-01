"""The IRIS pilot stages. Every stage is safe to rerun (see README, "Idempotency")."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from .dossier import RENDERERS
from .runner import Stage, utcnow

CRS = 25832
MIN_PG = 160000                   # PostgreSQL 16
MIN_POSTGIS = (3, 4)
LOCK_KEY = 22_061                 # advisory lock id: one pilot run at a time
FIXTURE_FILES = ("staging_sites.csv", "substations.csv", "commercial_assumptions.csv")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

VIEWS = {
    "bess": ("mart.bess_candidates", ("core.bess_sites", "core.grid_substations")),
    "peatland": ("mart.peatland_candidates", ("core.peatland_sites", "core.commercial_assumptions")),
}


@dataclass
class RunContext:
    dsn: str
    fixtures_dir: Path
    out_dir: Path
    run_id: str
    sample_limit: int = 5
    migrations_dir: Path = MIGRATIONS_DIR
    conn: psycopg.Connection | None = None

    def close(self) -> None:
        if self.conn is not None and not self.conn.closed:
            self.conn.close()          # also releases the advisory lock


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True, connect_timeout=5, row_factory=dict_row)


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".")[:2] if p.isdigit())

# ------------------------------------------------------------------ 1. preflight
def preflight(ctx: RunContext) -> dict:
    problems: list[str] = []
    for name in FIXTURE_FILES:
        if not (ctx.fixtures_dir / name).is_file():
            problems.append(f"missing fixture file: {ctx.fixtures_dir / name}")
    if not list(ctx.migrations_dir.glob("*.sql")):
        problems.append(f"no migrations found in {ctx.migrations_dir}")
    try:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        probe = ctx.out_dir / ".write_probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        problems.append(f"output dir not writable: {exc}")

    try:
        ctx.conn = connect(ctx.dsn)
    except psycopg.OperationalError as exc:
        problems.append(f"cannot connect to database: {str(exc).strip().splitlines()[0]}")
        raise RuntimeError("; ".join(problems))

    conn = ctx.conn
    server = conn.info.server_version
    if server < MIN_PG:
        problems.append(f"PostgreSQL {server // 10000} found, 16+ required")
    row = conn.execute("SELECT default_version FROM pg_available_extensions "
                       "WHERE name = 'postgis'").fetchone()
    if row is None:
        problems.append("PostGIS extension is not available on the server")
    elif _version_tuple(row["default_version"]) < MIN_POSTGIS:
        problems.append(f"PostGIS {row['default_version']} available, 3.4+ required")
    if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,)).fetchone()["ok"]:
        problems.append("another pilot run holds the run lock; refusing to run concurrently")

    if problems:
        raise RuntimeError("; ".join(problems))
    return {"counts": {"postgres_version": server,
                       "postgis_available": row["default_version"]}}

# -------------------------------------------------------------------- 2. migrate
def migrate(ctx: RunContext) -> dict:
    conn = ctx.conn
    with conn.transaction():
        conn.execute("CREATE SCHEMA IF NOT EXISTS ops")
        conn.execute("CREATE TABLE IF NOT EXISTS ops.schema_migrations ("
                     " version text PRIMARY KEY, checksum text NOT NULL,"
                     " applied_at timestamptz NOT NULL DEFAULT now())")
    applied = {r["version"]: r["checksum"] for r in
               conn.execute("SELECT version, checksum FROM ops.schema_migrations")}
    newly = []
    for path in sorted(ctx.migrations_dir.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode()).hexdigest()[:16]
        if path.stem in applied:
            if applied[path.stem] != checksum:
                raise RuntimeError(f"migration {path.name} was edited after being applied "
                                   f"(checksum mismatch); add a new migration instead")
            continue
        with conn.transaction():                 # each migration is all-or-nothing
            conn.execute(sql)
            conn.execute("INSERT INTO ops.schema_migrations (version, checksum) VALUES (%s, %s)",
                         (path.stem, checksum))
        newly.append(path.stem)
    return {"counts": {"applied_now": len(newly), "already_applied": len(applied)},
            "outputs": {"applied": newly}}

# --------------------------------------------------------------- 3. ingest fixtures
def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return [{k: (v if v != "" else None) for k, v in row.items()} for row in csv.DictReader(f)]


def ingest_fixtures(ctx: RunContext) -> dict:
    """Stand-in for source adapters: upsert local fixtures into staging / reference tables."""
    sites = _read_csv(ctx.fixtures_dir / "staging_sites.csv")
    subs = _read_csv(ctx.fixtures_dir / "substations.csv")
    assumptions = _read_csv(ctx.fixtures_dir / "commercial_assumptions.csv")
    conn = ctx.conn
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO staging.site_submissions AS t
                  (country_code, source_id, vertical, name, review_status, geom,
                   source_name, source_date, uncertainty, peat_depth_m)
                VALUES (%(country_code)s, %(source_id)s, %(vertical)s, %(name)s, %(review_status)s,
                        CASE WHEN %(geom_wkt)s::text IS NULL THEN NULL
                             -- no declared CRS -> SRID 0 (unknown); promotion will reject it
                             ELSE ST_GeomFromText(%(geom_wkt)s::text, coalesce(%(srid)s::int, 0)) END,
                        %(source_name)s, %(source_date)s, %(uncertainty)s, %(peat_depth_m)s)
                ON CONFLICT (country_code, source_id) DO UPDATE SET
                  vertical = excluded.vertical, name = excluded.name,
                  review_status = excluded.review_status, geom = excluded.geom,
                  source_name = excluded.source_name, source_date = excluded.source_date,
                  uncertainty = excluded.uncertainty, peat_depth_m = excluded.peat_depth_m
            """, sites)
            # Reference data goes straight to core; only touch rows that really changed so a
            # rerun does not bump change-tracking and make views look stale.
            cur.executemany("""
                INSERT INTO core.grid_substations AS t
                  (country_code, substation_id, name, voltage_kv, geom, source_name, source_date)
                VALUES (%(country_code)s, %(substation_id)s, %(name)s, %(voltage_kv)s,
                        ST_SetSRID(ST_MakePoint(%(x)s::float8, %(y)s::float8), %(srid)s::int),
                        %(source_name)s, %(source_date)s)
                ON CONFLICT (country_code, substation_id) DO UPDATE SET
                  name = excluded.name, voltage_kv = excluded.voltage_kv, geom = excluded.geom,
                  source_name = excluded.source_name, source_date = excluded.source_date
                WHERE (t.name, t.voltage_kv, ST_AsEWKB(t.geom), t.source_name, t.source_date)
                      IS DISTINCT FROM
                      (excluded.name, excluded.voltage_kv, ST_AsEWKB(excluded.geom),
                       excluded.source_name, excluded.source_date)
            """, subs)
            cur.executemany("""
                INSERT INTO core.commercial_assumptions AS t
                  (country_code, assumption_key, value, unit, basis)
                VALUES (%(country_code)s, %(assumption_key)s, %(value)s, %(unit)s, %(basis)s)
                ON CONFLICT (country_code, assumption_key) DO UPDATE SET
                  value = excluded.value, unit = excluded.unit, basis = excluded.basis
                WHERE (t.value, t.unit, t.basis) IS DISTINCT FROM
                      (excluded.value, excluded.unit, excluded.basis)
            """, assumptions)
    return {"counts": {"staging_rows": len(sites), "substations": len(subs),
                       "assumptions": len(assumptions)}}

# --------------------------------------------------------------------- 4. promote
_VALIDATE = f"""
    SELECT s.*,
      CASE
        WHEN s.geom IS NULL THEN 'missing geometry (not inferred)'
        WHEN ST_SRID(s.geom) <> {CRS}
             THEN format('CRS EPSG:%%s does not match contract EPSG:{CRS}', ST_SRID(s.geom))
        WHEN GeometryType(s.geom) NOT IN ('POLYGON', 'MULTIPOLYGON')
             THEN format('geometry type %%s is not polygonal', GeometryType(s.geom))
        WHEN NOT ST_IsValid(s.geom) THEN 'invalid geometry: ' || ST_IsValidReason(s.geom)
      END AS rejection_reason
    FROM staging.site_submissions s
    WHERE s.review_status = 'accepted' AND s.vertical = %(vertical)s
"""

_PROMOTE = {
    "bess": f"""
        WITH ok AS ({_VALIDATE}),
        up AS (
          INSERT INTO core.bess_sites AS t
            (country_code, source_id, name, geom, source_name, source_date, uncertainty)
          SELECT country_code, source_id, name, ST_Multi(geom), source_name, source_date, uncertainty
          FROM ok WHERE rejection_reason IS NULL
          ON CONFLICT (country_code, source_id) DO UPDATE SET
            name = excluded.name, geom = excluded.geom, source_name = excluded.source_name,
            source_date = excluded.source_date, uncertainty = excluded.uncertainty,
            updated_at = now()
          WHERE (t.name, ST_AsEWKB(t.geom), t.source_name, t.source_date, t.uncertainty)
                IS DISTINCT FROM
                (excluded.name, ST_AsEWKB(excluded.geom), excluded.source_name,
                 excluded.source_date, excluded.uncertainty)
          RETURNING (xmax = 0) AS inserted)
        SELECT count(*) FILTER (WHERE inserted) AS inserted,
               count(*) FILTER (WHERE NOT inserted) AS updated FROM up""",
    "peatland": f"""
        WITH ok AS ({_VALIDATE}),
        up AS (
          INSERT INTO core.peatland_sites AS t
            (country_code, source_id, name, geom, peat_depth_m, source_name, source_date, uncertainty)
          SELECT country_code, source_id, name, ST_Multi(geom), peat_depth_m, source_name,
                 source_date, uncertainty
          FROM ok WHERE rejection_reason IS NULL
          ON CONFLICT (country_code, source_id) DO UPDATE SET
            name = excluded.name, geom = excluded.geom, peat_depth_m = excluded.peat_depth_m,
            source_name = excluded.source_name, source_date = excluded.source_date,
            uncertainty = excluded.uncertainty, updated_at = now()
          WHERE (t.name, ST_AsEWKB(t.geom), t.peat_depth_m, t.source_name, t.source_date,
                 t.uncertainty)
                IS DISTINCT FROM
                (excluded.name, ST_AsEWKB(excluded.geom), excluded.peat_depth_m,
                 excluded.source_name, excluded.source_date, excluded.uncertainty)
          RETURNING (xmax = 0) AS inserted)
        SELECT count(*) FILTER (WHERE inserted) AS inserted,
               count(*) FILTER (WHERE NOT inserted) AS updated FROM up""",
}


def promote_accepted(ctx: RunContext) -> dict:
    """Promote accepted + valid staging rows to core. Invalid rows are reported, never repaired."""
    conn = ctx.conn
    counts, warnings, rejected = {}, [], []
    with conn.transaction():                               # both verticals or neither
        for vertical, sql in _PROMOTE.items():
            checked = conn.execute(_VALIDATE, {"vertical": vertical}).fetchall()
            bad = [r for r in checked if r["rejection_reason"]]
            res = conn.execute(sql, {"vertical": vertical}).fetchone()
            valid = len(checked) - len(bad)
            counts[vertical] = {"accepted": len(checked), "inserted": res["inserted"],
                                "updated": res["updated"],
                                "unchanged": valid - res["inserted"] - res["updated"],
                                "rejected": len(bad)}
            for r in bad:
                rejected.append({"country_code": r["country_code"], "source_id": r["source_id"],
                                 "vertical": vertical, "reason": r["rejection_reason"]})
                warnings.append(f"{r['country_code']}/{r['source_id']} not promoted: "
                                f"{r['rejection_reason']}")
    return {"counts": counts, "warnings": warnings, "outputs": {"rejected": rejected}}

# -------------------------------------------------------------- 5/6. refresh views
def _source_watermark(conn, tables) -> object:
    return conn.execute("SELECT max(changed_at) AS wm FROM ops.source_changes "
                        "WHERE table_name = ANY(%s)", (list(tables),)).fetchone()["wm"]


def make_refresh(vertical: str):
    view, inputs = VIEWS[vertical]

    def refresh(ctx: RunContext) -> dict:
        conn = ctx.conn
        # Refresh and bookkeeping commit together: the state row can never claim a refresh
        # that did not happen.
        with conn.transaction():
            wm = _source_watermark(conn, inputs)
            conn.execute(f"REFRESH MATERIALIZED VIEW {view}")
            n = conn.execute(f"SELECT count(*) AS n FROM {view}").fetchone()["n"]
            conn.execute("""
                INSERT INTO ops.pipeline_state
                  (object_name, completed_at, source_watermark, row_count, run_id)
                VALUES (%s, clock_timestamp(), %s, %s, %s)
                ON CONFLICT (object_name) DO UPDATE SET
                  completed_at = excluded.completed_at, source_watermark = excluded.source_watermark,
                  row_count = excluded.row_count, run_id = excluded.run_id
            """, (view, wm, n, ctx.run_id))
        return {"counts": {"rows": n}}

    refresh.__name__ = f"refresh_{vertical}_view"
    return refresh

# ------------------------------------------------------------------- 7/8. export
def make_export(vertical: str):
    view, _ = VIEWS[vertical]

    def export(ctx: RunContext) -> dict:
        conn = ctx.conn
        state = conn.execute("SELECT completed_at FROM ops.pipeline_state WHERE object_name = %s",
                             (view,)).fetchone()
        if state is None:
            raise RuntimeError(f"{view} has never been refreshed; refusing to export")
        rows = conn.execute(
            f"SELECT * FROM {view} ORDER BY screening_band, country_code, source_id LIMIT %s",
            (ctx.sample_limit,)).fetchall()
        if not rows:
            raise RuntimeError(f"{view} is empty; an export with zero dossiers is not a success")

        base = ctx.out_dir / "dossiers"
        target, tmp = base / vertical, base / f".{vertical}.tmp-{ctx.run_id}"
        base.mkdir(parents=True, exist_ok=True)
        for leftover in base.glob(f".{vertical}.tmp-*"):      # from a crashed earlier run
            shutil.rmtree(leftover, ignore_errors=True)
        tmp.mkdir()
        render = RENDERERS[vertical]
        files = []
        try:
            for r in rows:
                name = f"{r['country_code']}_{r['source_id']}.md"
                (tmp / name).write_text(render(r), encoding="utf-8")
                files.append(name)
            (tmp / "index.json").write_text(json.dumps({
                "vertical": vertical, "run_id": ctx.run_id, "exported_at": utcnow(),
                "view": view, "view_refreshed_at": str(state["completed_at"]), "files": files,
            }, indent=2), encoding="utf-8")
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)   # leave the previous export untouched
            raise

        # Swap directories so readers see either the old complete set or the new one.
        old = base / f".{vertical}.old-{ctx.run_id}"
        if target.exists():
            os.replace(target, old)
        os.replace(tmp, target)
        shutil.rmtree(old, ignore_errors=True)

        with conn.transaction():
            conn.execute("""
                INSERT INTO ops.pipeline_state
                  (object_name, completed_at, source_watermark, row_count, run_id)
                VALUES (%s, clock_timestamp(), %s, %s, %s)
                ON CONFLICT (object_name) DO UPDATE SET
                  completed_at = excluded.completed_at, source_watermark = excluded.source_watermark,
                  row_count = excluded.row_count, run_id = excluded.run_id
            """, (f"export:{vertical}", state["completed_at"], len(files), ctx.run_id))
        return {"counts": {"dossiers": len(files)},
                "outputs": {"dir": str(target), "files": files}}

    export.__name__ = f"export_{vertical}_dossiers"
    return export

# ------------------------------------------------------------------- freshness
def check_freshness(ctx: RunContext) -> dict:
    """Is each view / export up to date with its inputs? Works even after a failed run."""
    conn = ctx.conn if ctx.conn is not None and not ctx.conn.closed else connect(ctx.dsn)
    if conn.execute("SELECT to_regclass('ops.pipeline_state') AS t").fetchone()["t"] is None:
        return {f"{VIEWS[v][0]}": {"stale": True, "reasons": ["database not initialised"]}
                for v in VIEWS} | {f"export:{v}": {"stale": True,
                                                  "reasons": ["database not initialised"]}
                                   for v in VIEWS}
    state = {r["object_name"]: r for r in conn.execute("SELECT * FROM ops.pipeline_state")}
    result: dict = {}
    for vertical, (view, inputs) in VIEWS.items():
        reasons = []
        s = state.get(view)
        current_wm = _source_watermark(conn, inputs)
        if conn.execute("SELECT to_regclass(%s) AS t", (view,)).fetchone()["t"] is None:
            reasons.append("view does not exist")
        if s is None:
            reasons.append("never refreshed")
        elif current_wm is not None and (s["source_watermark"] is None
                                         or current_wm > s["source_watermark"]):
            reasons.append(f"inputs changed at {current_wm} after last refresh "
                           f"(built from state as of {s['source_watermark']})")
        result[view] = _entry(s, reasons)

        ex_reasons = []
        e = state.get(f"export:{vertical}")
        if e is None:
            ex_reasons.append("never exported")
        else:
            if s is not None and s["completed_at"] > e["source_watermark"]:
                ex_reasons.append(f"view refreshed at {s['completed_at']} after this export")
            if reasons:
                ex_reasons.append(f"upstream view {view} is stale")
            if not (ctx.out_dir / "dossiers" / vertical / "index.json").is_file():
                ex_reasons.append("export files missing on disk")
        result[f"export:{vertical}"] = _entry(e, ex_reasons)
    if conn is not ctx.conn:
        conn.close()
    return result


def _entry(state_row, reasons) -> dict:
    return {"stale": bool(reasons), "reasons": reasons,
            "last_completed_at": str(state_row["completed_at"]) if state_row else None,
            "last_run_id": state_row["run_id"] if state_row else None,
            "row_count": state_row["row_count"] if state_row else None}


# ------------------------------------------------------------------ the plan
def build_stages() -> list[Stage]:
    return [
        Stage("preflight", preflight, critical=True),
        Stage("migrate", migrate, critical=True, depends_on=("preflight",)),
        Stage("ingest_fixtures", ingest_fixtures, critical=True, depends_on=("migrate",)),
        Stage("promote_accepted", promote_accepted, critical=True, depends_on=("ingest_fixtures",)),
        # Per-vertical stages: a BESS failure must not hide peatland results (and vice versa),
        # but ANY failure still makes the whole run `failed`.
        Stage("refresh_bess_view", make_refresh("bess"), depends_on=("promote_accepted",)),
        Stage("refresh_peatland_view", make_refresh("peatland"), depends_on=("promote_accepted",)),
        Stage("export_bess_dossiers", make_export("bess"), depends_on=("refresh_bess_view",)),
        Stage("export_peatland_dossiers", make_export("peatland"),
              depends_on=("refresh_peatland_view",)),
    ]