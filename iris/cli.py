"""CLI.

    python -m iris run      # the one command: all stages, both verticals, writes manifest
    python -m iris status   # latest manifest + live staleness check

Exit codes: 0 = success, 1 = failed or stale.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .runner import ManifestWriter, new_run_id, run_pipeline
from .stages import RunContext, build_stages, check_freshness

DEFAULT_DSN = "postgresql://iris:iris@localhost:5433/iris"
ICON = {"success": "OK  ", "failed": "FAIL", "skipped": "SKIP"}


def _print_summary(m: dict) -> None:
    print(f"\nrun {m['run_id']}  ->  {m['status'].upper()}  ({m.get('duration_s', '?')} s)")
    for s in m["stages"]:
        detail = ""
        if s["status"] == "failed":
            detail = f"  {s['error']['type']}: {s['error']['message']}"
        elif s["status"] == "skipped":
            detail = f"  ({s['skipped_reason']})"
        elif s["counts"]:
            detail = "  " + json.dumps(s["counts"], default=str)
        print(f"  [{ICON[s['status']]}] {s['name']:<26}{detail}")
        for w in s.get("warnings", []):
            print(f"         warning: {w}")
    _print_freshness(m.get("freshness"))


def _print_freshness(fresh: dict | None) -> None:
    if not fresh:
        return
    print("  freshness:")
    if "error" in fresh:
        print(f"    could not check: {fresh['error']}")
        return
    for obj, f in fresh.items():
        label = "STALE" if f["stale"] else "fresh"
        why = f"  <- {'; '.join(f['reasons'])}" if f["reasons"] else ""
        print(f"    {label:<5} {obj:<30} last ok {f['last_completed_at']}{why}")


def cmd_run(args) -> int:
    run_id = new_run_id()
    ctx = RunContext(dsn=args.dsn, fixtures_dir=Path(args.fixtures), out_dir=Path(args.out),
                     run_id=run_id, sample_limit=args.limit)
    writer = ManifestWriter(Path(args.out) / "runs")
    try:
        result = run_pipeline(build_stages(), ctx, writer, run_id, freshness_check=check_freshness)
    finally:
        ctx.close()
    _print_summary(result.manifest)
    print(f"  manifest: {Path(args.out) / 'runs' / (run_id + '.json')}")
    return 0 if result.ok else 1


def cmd_status(args) -> int:
    latest = Path(args.out) / "runs" / "latest.json"
    if not latest.is_file():
        print("no runs recorded yet")
        return 1
    m = json.loads(latest.read_text(encoding="utf-8"))
    print(f"last run {m['run_id']}: {m['status'].upper()} (finished {m['finished_at']})")
    ctx = RunContext(dsn=args.dsn, fixtures_dir=Path("."), out_dir=Path(args.out), run_id="status")
    try:
        fresh = check_freshness(ctx)
    except Exception as exc:                      # noqa: BLE001
        fresh = {"error": f"{type(exc).__name__}: {exc}"}
    print("live check (now):")
    _print_freshness(fresh)
    stale = "error" in fresh or any(v["stale"] for v in fresh.values())
    return 0 if (m["status"] == "success" and not stale) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="iris", description="IRIS pilot orchestration")
    p.add_argument("--dsn", default=os.environ.get("IRIS_DSN", DEFAULT_DSN))
    p.add_argument("--out", default="out", help="output directory (dossiers + run manifests)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run all stages")
    r.add_argument("--fixtures", default="fixtures")
    r.add_argument("--limit", type=int, default=5, help="sample dossiers per vertical")
    sub.add_parser("status", help="show last run and live staleness")
    args = p.parse_args(argv)
    return cmd_run(args) if args.cmd == "run" else cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())