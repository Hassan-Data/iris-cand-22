"""Each DB test gets its own throwaway database, so tests never interfere with each other
or with your working `iris` database. Needs a running Postgres+PostGIS (see README)."""
import os
import uuid
from pathlib import Path

import psycopg
import pytest

from iris.runner import ManifestWriter, new_run_id, run_pipeline
from iris.stages import RunContext, build_stages, check_freshness

ADMIN_DSN = os.environ.get("IRIS_TEST_DSN", "postgresql://iris:iris@localhost:5433/postgres")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db_dsn():
    name = f"iris_test_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no test database available at {ADMIN_DSN}: {exc}")
    admin.execute(f'CREATE DATABASE "{name}"')
    yield psycopg.conninfo.make_conninfo(ADMIN_DSN, dbname=name)
    admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture
def run(db_dsn, tmp_path):
    """Call run() to execute the full pipeline against the throwaway DB."""
    def _run(stages=None, fixtures=ROOT / "fixtures"):
        run_id = new_run_id()
        ctx = RunContext(dsn=db_dsn, fixtures_dir=Path(fixtures), out_dir=tmp_path / "out",
                         run_id=run_id)
        try:
            return run_pipeline(stages or build_stages(), ctx,
                                ManifestWriter(tmp_path / "out" / "runs"), run_id,
                                freshness_check=check_freshness).manifest
        finally:
            ctx.close()
    _run.out = tmp_path / "out"
    _run.dsn = db_dsn
    return _run