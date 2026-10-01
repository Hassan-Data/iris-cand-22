"""Generic ordered stage runner + run manifest.

Rules (these are the guarantees the tests check):
  * Stages run in the listed order.
  * A stage runs only if every stage in `depends_on` succeeded; otherwise it is `skipped`
    with the reason recorded.
  * If a `critical` stage fails, every later stage is `skipped` (the run is aborted).
  * The overall status is `success` ONLY if every stage succeeded AND the freshness check
    reports nothing stale. Anything else is `failed`. There is no third "partly ok" state.
  * The manifest is written before the first stage (status `running`) and after every
    stage, so a crash or kill can never leave a manifest that claims success.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


@dataclass(frozen=True)
class Stage:
    name: str
    fn: Callable[[Any], dict | None]   # returns {"counts": {...}, "warnings": [...], "outputs": {...}}
    critical: bool = False
    depends_on: tuple[str, ...] = ()


@dataclass
class ManifestWriter:
    runs_dir: Path

    def write(self, manifest: dict) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(manifest, indent=2, default=str)
        for name in (f"{manifest['run_id']}.json", "latest.json"):
            tmp = self.runs_dir / f".{name}.tmp"
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.runs_dir / name)          # atomic: never a half-written manifest


@dataclass
class RunResult:
    manifest: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.manifest.get("status") == "success"


def run_pipeline(
        stages: list[Stage],
        ctx: Any,
        writer: ManifestWriter,
        run_id: str,
        freshness_check: Callable[[Any], dict] | None = None,
) -> RunResult:
    manifest: dict = {
        "run_id": run_id,
        "status": "running",
        "started_at": utcnow(),
        "finished_at": None,
        "command": " ".join(sys.argv),
        "stages": [],
        "freshness": None,
        "stale_objects": [],
    }
    writer.write(manifest)
    t_run = time.monotonic()
    statuses: dict[str, str] = {}
    aborted_by: str | None = None

    try:
        for stage in stages:
            rec: dict = {"name": stage.name, "critical": stage.critical,
                         "depends_on": list(stage.depends_on), "status": None,
                         "started_at": None, "finished_at": None, "duration_s": None,
                         "counts": {}, "warnings": [], "outputs": {}, "error": None,
                         "skipped_reason": None}
            blocked = [d for d in stage.depends_on if statuses.get(d) != "success"]

            if aborted_by:
                rec["status"] = "skipped"
                rec["skipped_reason"] = f"run aborted: critical stage '{aborted_by}' failed"
            elif blocked:
                rec["status"] = "skipped"
                rec["skipped_reason"] = f"upstream stage(s) not successful: {', '.join(blocked)}"
            else:
                rec["started_at"] = utcnow()
                t0 = time.monotonic()
                try:
                    out = stage.fn(ctx) or {}
                    rec["counts"] = out.get("counts", {})
                    rec["warnings"] = out.get("warnings", [])
                    rec["outputs"] = out.get("outputs", {})
                    rec["status"] = "success"
                except Exception as exc:  # noqa: BLE001 - we record everything
                    rec["status"] = "failed"
                    rec["error"] = {"type": type(exc).__name__, "message": str(exc),
                                    "trace": traceback.format_exc(limit=3)}
                    if stage.critical:
                        aborted_by = stage.name
                rec["finished_at"] = utcnow()
                rec["duration_s"] = round(time.monotonic() - t0, 3)

            statuses[stage.name] = rec["status"]
            manifest["stages"].append(rec)
            writer.write(manifest)

        if freshness_check is not None:
            try:
                manifest["freshness"] = freshness_check(ctx)
            except Exception as exc:  # noqa: BLE001
                manifest["freshness"] = {"error": f"{type(exc).__name__}: {exc}"}

        fresh = manifest["freshness"] or {}
        manifest["stale_objects"] = sorted(k for k, v in fresh.items()
                                           if isinstance(v, dict) and v.get("stale"))
        all_ok = all(s == "success" for s in statuses.values()) and len(statuses) == len(stages)
        freshness_ok = freshness_check is None or (
                "error" not in fresh and not manifest["stale_objects"])
        manifest["status"] = "success" if (all_ok and freshness_ok) else "failed"
    finally:
        if manifest["status"] == "running":  # interrupted (Ctrl-C, SystemExit, ...)
            manifest["status"] = "failed"
            manifest["error"] = "run interrupted before completion"
        manifest["finished_at"] = utcnow()
        manifest["duration_s"] = round(time.monotonic() - t_run, 3)
        writer.write(manifest)

    return RunResult(manifest)