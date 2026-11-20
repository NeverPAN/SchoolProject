"""Import a trusted, checksummed catalog into an isolated SQLite experiment service.

Run with PYTHONPATH pointing to the selected backend and an explicit RECON_ENV_FILE.
This is an operator CLI, not an upload endpoint and not a reconstruction worker.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from sqlalchemy import select

from app.db.base import utcnow_db
from app.models.entities import Model, Project, ReconstructionJob, User
from app.reconstruction.manifest import atomic_write_json, sha256_file
from app.services.model_service import ensure_version, export_stats

PROJECT_NAME = "优化实验验收"
TYPES = {"sparse_pointcloud": ".ply", "dense_pointcloud": ".ply", "mesh": ".ply", "web": ".glb"}


def checked_source(root: Path, entry: dict) -> Path:
    rel = Path(entry["path"])
    path = (root / rel).resolve()
    if (rel.is_absolute() or not path.is_relative_to(root.resolve()) or not path.is_file()
            or path.suffix != TYPES.get(entry["type"]) or path.stat().st_size == 0):
        raise ValueError(f"Invalid artifact: {rel}")
    return path


def import_catalog(db, owner: User, root: Path, storage: Path, catalog: list[dict]) -> dict:
    codes = set()
    for run in catalog:
        if (not re.fullmatch(r"[A-Z][0-9]{2}", run["code"]) or run["code"] in codes
                or not 1 <= len(f'{run["code"]} · {run["name"]}') <= 120 or not run["files"]):
            raise ValueError("Invalid or duplicate experiment")
        codes.add(run["code"])
    if db.scalar(select(Project.id).where(Project.owner_id == owner.id, Project.name == PROJECT_NAME)):
        raise ValueError("Experiment project already exists; use a fresh database or owner")
    project = Project(owner_id=owner.id, name=PROJECT_NAME,
                      description="离线产物，仅供对照，不替换现有模型")
    created = []
    result = {}
    try:
        db.add(project)
        db.flush()
        for run in catalog:
            job = ReconstructionJob(project_id=project.id, status="SUCCEEDED", progress=100,
                                    pipeline_mode="experiment", experiment_name=f'{run["code"]} · {run["name"]}',
                                    finished_at=utcnow_db(), config_json=json.dumps(dict(
                                        preset="standard", matcher="auto", use_gpu=False,
                                        enable_dense=False, enable_mesh=False, mode="experiment")))
            db.add(job)
            db.flush()
            dest = storage / "users" / str(owner.id) / "projects" / str(project.id) / "outputs" / f"job-{job.id}"
            dest.mkdir(parents=True, exist_ok=False)
            created.append(dest)
            models = []
            artifacts = []
            for entry in run["files"]:
                src = checked_source(root, entry)
                dst = dest / src.name
                with src.open("rb") as inp, dst.open("xb") as out:
                    shutil.copyfileobj(inp, out)
                digest = sha256_file(dst)
                if digest != entry["sha256"]:
                    raise ValueError(f"Hash mismatch: {src.name}")
                fmt = dst.suffix[1:]
                stats = export_stats(dst, fmt)
                if not stats["vertex_count"] or not stats["face_count" if entry["type"] in {"web", "mesh"} else "point_count"]:
                    raise ValueError(f"Unexpected or empty geometry: {src.name}")
                model = Model(project_id=project.id, job_id=job.id, model_type=entry["type"], format=fmt,
                              file_path=str(dst), file_size=dst.stat().st_size, stage_origin=entry["origin"], **stats)
                db.add(model)
                db.flush()
                ensure_version(db, model, change_type="manual", description="实验归档；来源和对照见任务清单")
                models.append(model.id)
                artifacts.append(dict(name=dst.name, model_id=model.id, sha256=digest,
                                      source_model_id=entry.get("source_model_id"), bytes=model.file_size, **stats))
            metadata = {key: run[key] for key in ("code", "parent", "scope", "source", "parameters", "metrics")}
            atomic_write_json(dest / "job_manifest.json", {**run.get("display", {}), "job_id": job.id,
                              "experiment": metadata, "artifacts": artifacts})
            result[run["code"]] = {"job": job.id, "models": models, "artifacts": artifacts}
        db.commit()
    except Exception:
        db.rollback()
        for dest in created:
            shutil.rmtree(dest)
        raise
    return {"project": project.id, "experiments": result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    args = parser.parse_args()
    from app.core.config import get_settings

    runtime = args.runtime.resolve()
    settings = get_settings()
    if (not (runtime / "experiment-runtime.marker").is_file()
            or settings.database_url != f"sqlite:///{runtime / 'app.db'}"
            or Path(settings.data_dir).resolve() != runtime / "data"
            or Path(settings.scratch_dir).resolve() != runtime / "scratch"):
        parser.error("requires explicitly prepared isolated experiment runtime")
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        owner = db.scalar(select(User).where(User.username == args.owner))
        if owner is None:
            parser.error("owner must already exist in the isolated database")
        result = import_catalog(db, owner, args.assets, Path(settings.data_dir), json.loads(args.catalog.read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
