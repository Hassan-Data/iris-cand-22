"""Runner rules, tested with fake stages (no database needed)."""
import json

import pytest

from iris.runner import ManifestWriter, Stage, run_pipeline


def ok(ctx):
    ctx.append("ran")
    return {"counts": {"n": 1}}


def boom(ctx):
    raise ValueError("boom")


def _run(stages, tmp_path, freshness=None):
    return run_pipeline(stages, [], ManifestWriter(tmp_path), "r1", freshness_check=freshness).manifest


def by_name(m):
    return {s["name"]: s for s in m["stages"]}


def test_all_ok_is_success(tmp_path):
    m = _run([Stage("a", ok), Stage("b", ok, depends_on=("a",))], tmp_path)
    assert m["status"] == "success"
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["status"] == "success"


def test_critical_failure_aborts_rest_and_run_fails(tmp_path):
    m = _run([Stage("a", boom, critical=True), Stage("b", ok), Stage("c", ok)], tmp_path)
    s = by_name(m)
    assert m["status"] == "failed"
    assert s["a"]["status"] == "failed" and s["a"]["error"]["message"] == "boom"
    assert s["b"]["status"] == s["c"]["status"] == "skipped"
    assert "critical stage 'a' failed" in s["b"]["skipped_reason"]


def test_noncritical_failure_skips_dependents_only_but_run_still_fails(tmp_path):
    m = _run([Stage("x", boom), Stage("x_child", ok, depends_on=("x",)), Stage("y", ok)], tmp_path)
    s = by_name(m)
    assert s["x_child"]["status"] == "skipped"
    assert s["y"]["status"] == "success"
    assert m["status"] == "failed"          # never "success" with a failed stage


def test_stale_output_prevents_success(tmp_path):
    m = _run([Stage("a", ok)], tmp_path, freshness=lambda ctx: {"v": {"stale": True, "reasons": ["x"]}})
    assert m["status"] == "failed" and m["stale_objects"] == ["v"]


def test_interrupted_run_is_recorded_as_failed(tmp_path):
    def interrupt(ctx):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        _run([Stage("a", ok), Stage("b", interrupt)], tmp_path)
    m = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert m["status"] == "failed" and "interrupted" in m["error"]