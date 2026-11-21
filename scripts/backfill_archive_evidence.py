"""Backfill the approved experiment archive in an offline, isolated SQLite copy.

Use the repository backend on PYTHONPATH and an explicit RECON_ENV_FILE. The
source databases are read-only snapshots. No workers or reconstruction run.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.models.entities import JobStageEvent, Project, User
from app.reconstruction.manifest import input_manifest_sha, sha256_file
from import_experiments import PROJECT_NAME

# Original job ID and input snapshot digest from the approved archive catalogs.
COMPLETE_RUNS = {
    "B00": (13, "1f7a33f3771a66181309525bc916012822c398799d782f34999adf8ebd5b519a"),
    "C00": (1, "7e470fa74f23ee90f45405306c935d8824ecea304e281b488e1af075ba535b35"),
    "C01": (2, "ab3cc7a782446235745103b9a4f0c4419bce5bfe180833f13f364137b57d8f95"),
}
FULL_LOG_NAME = re.compile(r"(?:job|stage-[a-z_]+(?:-attempt-\d+)?)\.log")
EVENT_FIELDS = ("stage", "status", "started_at", "finished_at", "duration_ms", "message")
RUN_FIELDS = ("current_stage", "engine_version", "config_json", "started_at", "finished_at")
# These are job/stage times only, not the archive row's creation timestamp.
TIME_FIELDS = ("started_at", "finished_at")


def retained_log(root: Path, name: str) -> Path:
    root = root.resolve(strict=True)
    path = (root / name).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file() or not path.stat().st_size:
        raise ValueError(f"Missing or invalid retained log: {root / name}")
    return path


def row_fields(row, fields):
    return {key: datetime.fromisoformat(row[key]) if key in TIME_FIELDS and row[key] else row[key]
            for key in fields}


def complete_run(code, source_db, *, log_dir=None, scratch=None):
    import json

    source_id, digest = COMPLETE_RUNS[code]
    with closing(sqlite3.connect(source_db.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as src:
        src.row_factory = sqlite3.Row
        src.execute("BEGIN")
        row = src.execute("""
            SELECT j.*, p.owner_id FROM reconstruction_jobs j JOIN projects p ON p.id = j.project_id
            WHERE j.id = ? AND j.pipeline_mode = 'colmap' AND j.status = 'SUCCEEDED'
        """, (source_id,)).fetchone()
        if row is None or input_manifest_sha(json.loads(row["input_manifest"])) != digest:
            raise ValueError(f"{code}: original job/input snapshot does not match archive catalog")
        events = [row_fields(event, EVENT_FIELDS) for event in src.execute(
            "SELECT * FROM job_stage_events WHERE job_id = ? ORDER BY id", (source_id,))]
        if not row["started_at"] or not row["finished_at"] or not events:
            raise ValueError(f"{code}: missing original job or stage timing evidence")
        if log_dir is None:
            log_dir = scratch / "users" / str(row["owner_id"]) / "projects" / str(row["project_id"]) / "jobs" / str(source_id) / "logs"
    logs = {p.name: retained_log(log_dir, p.name) for p in log_dir.iterdir() if FULL_LOG_NAME.fullmatch(p.name)}
    if "job.log" not in logs:
        raise ValueError(f"{code}: missing original job.log in {log_dir}")
    return row_fields(row, RUN_FIELDS), events, logs


def stage_only(root, specs):
    events = []
    logs = {}
    for stage, name, message, cli_duration in specs:
        path = retained_log(root, name)
        duration = None
        if cli_duration:
            # COLMAP reports this duration in minutes; it is not end-to-end runtime.
            elapsed = re.findall(r"Elapsed time: (\d+\.\d+) \[minutes\]", path.read_text(encoding="utf-8", errors="replace"))
            if not elapsed:
                raise ValueError(f"No recorded CLI duration in {path}")
            duration = int(Decimal(elapsed[-1]) * 60000)
            message += "（COLMAP CLI 日志计时）"
        events.append(dict(stage=stage, status="completed", started_at=None, finished_at=None,
                           duration_ms=duration, message=message))
        logs[f"stage-{stage.lower()}.log"] = path
    # Older imports stored the import time in finished_at. It was not run timing.
    return dict(started_at=None, finished_at=None, engine_version=None, current_stage=None), events, logs


def approved_evidence(args):
    result = {
        "B00": complete_run("B00", args.baseline_db, log_dir=args.baseline_logs),
        "C00": complete_run("C00", args.combined_db, scratch=args.combined_scratch),
        "C01": complete_run("C01", args.combined_db, scratch=args.combined_scratch),
        "T01": stage_only(args.optimization_round, [
            ("TEXTURING", "texture-direct-attempt-3.log", "单独贴图重打包：0.125 档成功尝试", False),
        ]),
        "R01": stage_only(args.roof, [
            ("MESHING", "trim-9.log", "固定 C01 点云的 Poisson 网格，仅记录 trim=9 裁剪输出", False),
            ("TEXTURING", "texture9-attempt-1.log", "使用原照片给 trim=9 网格重新贴图", False),
        ]),
    }
    for code, suffix in {"F01": "56-repeat", "F02": "32", "F03": "16", "F04": "16-repeat"}.items():
        result[code] = stage_only(args.optimization_round / "fusion-sweep", [
            ("STEREO_FUSION", f"fusion-{suffix}.log", "固定深度图，单独测试点云融合", True),
        ])
    for code, iterations in {"S00": 5, "S01": 3}.items():
        result[code] = stage_only(args.optimization_round / "south-short", [
            ("DENSE_RECONSTRUCTION", f"patch-{iterations}.log", "South 12 视图 PatchMatch 短测", True),
            ("STEREO_FUSION", f"fusion-{iterations}.log", "South 12 视图短测的点云融合", True),
        ])
    for code, arm in {"L00": "control", "L01": "candidate"}.items():
        result[code] = stage_only(args.local / arm, [
            ("DENSE_RECONSTRUCTION", "patch.log", "旧照对照的局部深度图" if code == "L00" else "加入注册新照片后的局部深度图", True),
            ("STEREO_FUSION", "fusion.log", "局部深度图的 COLMAP 点云融合；归档结果仅为独立补点候选（不更新网格）", True),
        ])
    return result


def backfill(db, owner, scratch, evidence):
    project = db.scalars(select(Project).where(Project.owner_id == owner.id, Project.name == PROJECT_NAME)).one()
    archive_jobs = [j for j in project.jobs if j.pipeline_mode == "experiment"]
    matches = [(j.experiment_name.split(" · ", 1)[0], j) for j in archive_jobs
               if j.experiment_name and " · " in j.experiment_name]
    jobs = {code: job for code, job in matches if code in evidence}
    if (sum(code in evidence for code, _ in matches) != len(jobs) or set(jobs) != set(evidence)
            or any(j.status != "SUCCEEDED" for j in jobs.values())):
        raise ValueError("Expected one successful archive job for each approved experiment code")
    scratch = scratch.resolve(strict=True)
    pending = []
    result = {}
    for code, (run_fields, events, logs) in evidence.items():
        job = jobs[code]
        target = scratch / "users" / str(owner.id) / "projects" / str(project.id) / "jobs" / str(job.id) / "logs"
        if target.resolve() != target:
            raise ValueError(f"Symlink in destination path: {target}")
        if job.stage_events or target.exists():
            same_logs = (target.is_dir() and {p.name for p in target.iterdir()} == set(logs)
                         and all(not (target / name).is_symlink() and sha256_file(target / name) == sha256_file(src)
                                 for name, src in logs.items()))
            same_events = [{key: getattr(event, key) for key in EVENT_FIELDS} for event in job.stage_events] == events
            if not (same_logs and same_events and all(getattr(job, key) == value for key, value in run_fields.items())):
                raise ValueError(f"{code}: refusing to replace existing or conflicting archive evidence")
        else:
            pending.append((job, target, run_fields, events, logs))
        result[code] = dict(job_id=job.id, stages=len(events), logs=len(logs), original_run_times=code in COMPLETE_RUNS)

    installed = []
    try:
        for job, target, run_fields, events, logs in pending:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Prepare the entire log directory before publishing it.
            with tempfile.TemporaryDirectory(prefix=".archive-evidence-", dir=target.parent) as tmp:
                files = Path(tmp) / "logs"
                files.mkdir()
                for name, source in logs.items():
                    shutil.copyfile(source, files / name)
                files.rename(target)
                installed.append(target)
            for key, value in run_fields.items():
                setattr(job, key, value)
            for event in events:
                path = target / f'stage-{event["stage"].lower()}.log'
                db.add(JobStageEvent(job_id=job.id, log_path=str(path) if path.name in logs else None, **event))
        db.commit()
    except Exception:
        db.rollback()
        for path in installed:
            shutil.rmtree(path)
        raise
    return result


def main():
    import json
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    for name in ("runtime", "baseline-db", "baseline-logs", "combined-db", "combined-scratch", "optimization-round", "roof", "local"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    from app.core.config import get_settings

    runtime = args.runtime.resolve()
    settings = get_settings()
    target_db = runtime / "app.db"
    if (not os.environ.get("RECON_ENV_FILE") or not (runtime / "experiment-runtime.marker").is_file()
            or not target_db.is_file() or target_db.is_symlink()
            or settings.database_url != f"sqlite:///{target_db}"
            or Path(settings.data_dir).resolve() != runtime / "data"
            or Path(settings.scratch_dir).resolve() != runtime / "scratch"):
        parser.error("requires a previously prepared offline copy of the isolated experiment runtime")
    for source in (args.baseline_db, args.combined_db):
        if source.samefile(target_db):
            parser.error("source and destination databases must differ")
    evidence = approved_evidence(args)
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        owner = db.scalar(select(User).where(User.username == args.owner))
        if owner is None:
            parser.error("owner must already exist in the copied archive database")
        result = backfill(db, owner, runtime / "scratch", evidence)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
