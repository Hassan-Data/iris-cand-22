"""End-to-end tests against a real PostgreSQL/PostGIS."""
import shutil

import psycopg
import pytest

from iris import stages as st


def by_name(m):
    return {s["name"]: s for s in m["stages"]}


def read_dossiers(out):
    return {p.relative_to(out).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted((out / "dossiers").rglob("*.md"))}


def test_one_command_produces_both_verticals(run):
    m = run()
    assert m["status"] == "success", m
    assert all(s["status"] == "success" for s in m["stages"])
    assert m["stale_objects"] == []
    d = read_dossiers(run.out)
    assert any(k.startswith("dossiers/bess/") for k in d)
    assert any(k.startswith("dossiers/peatland/") for k in d)
    assert all("not certified compensation" in text for text in d.values())  # disclaimer everywhere


def test_invalid_rows_are_rejected_not_repaired(run):
    m = run()
    promo = by_name(m)["promote_accepted"]
    reasons = {r["source_id"]: r["reason"] for r in promo["outputs"]["rejected"]}
    assert reasons["DE-BESS-005"].startswith("missing geometry")
    assert "EPSG:4326" in reasons["DE-BESS-006"]
    assert reasons["DE-PEAT-003"].startswith("invalid geometry")
    # pending / review-rejected rows never reach core
    with psycopg.connect(run.dsn) as c:
        ids = {r[0] for r in c.execute("SELECT source_id FROM core.bess_sites UNION ALL "
                                       "SELECT source_id FROM core.peatland_sites")}
    assert ids.isdisjoint({"DE-BESS-004", "DE-PEAT-004", "DE-BESS-005", "DE-BESS-006", "DE-PEAT-003"})


def test_rerun_is_safe_and_deterministic(run):
    first = run()
    files_1 = read_dossiers(run.out)
    second = run()
    assert first["status"] == second["status"] == "success"
    p = by_name(second)["promote_accepted"]["counts"]
    assert p["bess"]["inserted"] == p["bess"]["updated"] == 0
    assert p["peatland"]["inserted"] == p["peatland"]["updated"] == 0
    assert read_dossiers(run.out) == files_1            # byte-identical, no duplicates
    with psycopg.connect(run.dsn) as c:
        assert c.execute("SELECT count(*) FROM core.bess_sites").fetchone()[0] == 3
        assert c.execute("SELECT count(*) FROM ops.schema_migrations").fetchone()[0] == 2


def test_failing_refresh_stage_marks_run_failed_and_export_stale(run):
    assert run()["status"] == "success"
    with psycopg.connect(run.dsn, autocommit=True) as c:
        c.execute("DROP MATERIALIZED VIEW mart.peatland_candidates")
    m = run()
    s = by_name(m)
    assert m["status"] == "failed"
    assert s["refresh_peatland_view"]["status"] == "failed"
    assert "does not exist" in s["refresh_peatland_view"]["error"]["message"]
    assert s["export_peatland_dossiers"]["status"] == "skipped"
    assert s["export_bess_dossiers"]["status"] == "success"      # other vertical unaffected
    assert {"mart.peatland_candidates", "export:peatland"} <= set(m["stale_objects"])


def test_failed_critical_preflight_aborts_everything(run, tmp_path):
    broken = tmp_path / "fixtures"
    shutil.copytree(st.MIGRATIONS_DIR.parent / "fixtures", broken)
    (broken / "substations.csv").unlink()
    m = run(fixtures=broken)
    s = by_name(m)
    assert m["status"] == "failed"
    assert s["preflight"]["status"] == "failed"
    assert "substations.csv" in s["preflight"]["error"]["message"]
    assert all(x["status"] == "skipped" for x in m["stages"][1:])


def test_export_failure_keeps_previous_dossiers_intact(run, monkeypatch):
    run()
    before = read_dossiers(run.out)
    original = st.RENDERERS["bess"]
    calls = {"n": 0}

    def flaky(row):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full (simulated)")
        return original(row)
    monkeypatch.setitem(st.RENDERERS, "bess", flaky)
    m = run()
    assert m["status"] == "failed"
    assert by_name(m)["export_bess_dossiers"]["status"] == "failed"
    assert read_dossiers(run.out) == before          # no half-written export replaced the old one


def test_manual_edit_makes_view_and_export_visibly_stale(run):
    run()
    with psycopg.connect(run.dsn, autocommit=True) as c:
        c.execute("UPDATE core.grid_substations SET voltage_kv = 380 WHERE substation_id = 'DE-SS-01'")
    ctx = st.RunContext(dsn=run.dsn, fixtures_dir=run.out, out_dir=run.out, run_id="check")
    fresh = st.check_freshness(ctx)
    assert fresh["mart.bess_candidates"]["stale"] is True
    assert fresh["export:bess"]["stale"] is True
    assert fresh["mart.peatland_candidates"]["stale"] is False