"""模型服务 — 登记、统计、版本管理。"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.entities import Model, ModelVersion, ReconstructionJob
from app.reconstruction import export as export_tools
from app.services.image_service import project_output_dir


def register_job_models(db: Session, job: ReconstructionJob) -> list[Model]:
    """任务导出完成后登记模型（幂等：重复调用会先清理该任务旧登记及其版本）。"""
    from app.models.entities import Model as M
    from app.models.entities import ModelVersion

    old = list(db.scalars(select(M).where(M.job_id == job.id)))
    if old:
        old_ids = [m.id for m in old]
        for v in db.scalars(select(ModelVersion).where(ModelVersion.model_id.in_(old_ids))):
            db.delete(v)
        for m in old:
            db.delete(m)
        db.flush()

    out_dir = project_output_dir(job.project, job.id)
    exported_json = (
        Path(db_path_of(job)) / "checkpoints" / "EXPORTING" / "exported.json"
    )
    items: list[dict] = []
    if exported_json.is_file():
        items = json.loads(exported_json.read_text(encoding="utf-8")).get("exported", [])
    else:
        # 兜底：扫描输出目录
        for f in ("sparse_pointcloud.ply", "dense_pointcloud.ply", "mesh.ply", "model.glb"):
            if (out_dir / f).is_file():
                t = {"sparse_pointcloud.ply": "sparse_pointcloud", "dense_pointcloud.ply": "dense_pointcloud",
                     "mesh.ply": "mesh", "model.glb": "web"}[f]
                items.append({"type": t, "format": Path(f).suffix.lstrip("."), "path": f, "origin": "EXPORTING"})

    registered: list[Model] = []
    for item in items:
        f = out_dir / item["path"]
        if not f.is_file() or f.stat().st_size == 0:
            continue
        stats = _safe_stats(f, item["format"])
        m = Model(
            project_id=job.project_id,
            job_id=job.id,
            model_type=item["type"],
            format=item["format"],
            file_path=str(f),
            file_size=f.stat().st_size,
            stage_origin=item.get("origin"),
            **stats,
        )
        db.add(m)
        registered.append(m)
    db.commit()
    return registered


def _safe_stats(f: Path, fmt: str) -> dict:
    try:
        if fmt == "ply":
            return export_tools.ply_stats(f)
        if fmt == "glb":
            import trimesh

            scene = trimesh.load(f, force="mesh", process=False)
            return {
                "vertex_count": int(len(scene.vertices)),
                "face_count": int(len(scene.faces)),
                "point_count": 0,
            }
    except Exception:
        pass
    return {"vertex_count": 0, "face_count": 0, "point_count": 0}


def db_path_of(job: ReconstructionJob) -> str:
    from app.services.image_service import project_job_dir

    return str(project_job_dir(job.project, job.id))


def ensure_version(
    db: Session,
    model: Model,
    change_type: str = "initial",
    description: str = "",
    manifest_path: str | None = None,
) -> ModelVersion:
    """为模型创建版本记录（同一模型首版 initial；后续 append_images/manual）。"""
    existing = db.scalar(
        select(func.max(ModelVersion.version_number)).where(ModelVersion.model_id == model.id)
    )
    ver = ModelVersion(
        project_id=model.project_id,
        model_id=model.id,
        version_number=(existing or 0) + 1,
        parent_version_id=None,
        change_type=change_type,
        change_description=description,
        manifest_path=manifest_path,
    )
    db.add(ver)
    db.commit()
    db.refresh(ver)
    return ver


def get_model_checked(db: Session, model_id: int, user_id: int) -> Model:
    m = db.get(Model, model_id)
    if not m:
        raise NotFoundError("模型不存在")
    if m.project.owner_id != user_id:
        raise NotFoundError("模型不存在")
    return m


def job_manifest_path(job: ReconstructionJob) -> str:
    """任务级 manifest（输出清单 + 输入清单 + 配置），存数据目录。"""
    out_dir = project_output_dir(job.project, job.id)
    return str(out_dir / "job_manifest.json")
