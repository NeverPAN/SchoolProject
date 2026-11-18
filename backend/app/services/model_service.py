"""模型服务 — 登记、统计、版本管理。"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.models.entities import Model, ModelVersion, ReconstructionJob
from app.services.image_service import project_output_dir


class InvalidExportError(AppError):
    code = "INVALID_EXPORT"
    status_code = 500


_EXPORT_FORMATS = {
    "sparse_pointcloud": {"ply", "glb"}, "dense_pointcloud": {"ply", "glb"},
    "mesh": {"ply", "glb"}, "web": {"glb"}, "preview": {"png"},
}


def prepare_job_models(job: ReconstructionJob) -> list[dict]:
    """Validate the explicit export list. Never publish files discovered by scanning."""
    out_dir = project_output_dir(job.project, job.id).resolve()
    exported_json = Path(db_path_of(job)) / "checkpoints" / "EXPORTING" / "exported.json"
    records: list[dict] = []
    seen = set()
    try:
        items = json.loads(exported_json.read_text(encoding="utf-8"))["exported"]
        if not isinstance(items, list):
            raise ValueError("exported must be a list")
        for item in items:
            name, kind, fmt = item["path"], item["type"], item["format"]
            relative = Path(name)
            if relative.name != name or relative.suffix.lower() != f".{fmt}" or name in seen:
                raise ValueError(f"Invalid or duplicate export path: {name}")
            seen.add(name)
            path = out_dir / relative
            if fmt not in _EXPORT_FORMATS.get(kind, ()) or path.is_symlink() or not path.is_file():
                raise ValueError(f"Missing or invalid export: {name}")
            size = path.stat().st_size
            if not size:
                raise ValueError(f"Empty export: {name}")
            stats = export_stats(path, fmt)
            if kind != "preview" and (not stats["vertex_count"] or kind == "mesh" and not stats["face_count"]):
                raise ValueError(f"Empty geometry: {name}")
            records.append(dict(model_type=kind, format=fmt, file_path=str(path), file_size=size,
                                stage_origin=item.get("origin"), **stats))
        if not any(record["model_type"] != "preview" for record in records):
            raise ValueError("No usable geometry was exported")
    except Exception as exc:
        raise InvalidExportError(f"导出产物无效: {exc}") from exc
    return records


def register_job_models(db: Session, job: ReconstructionJob, records: list[dict] | None = None) -> list[Model]:
    """Replace registrations within the caller's publication transaction."""
    if records is None:
        records = prepare_job_models(job)
    old = list(db.scalars(select(Model).where(Model.job_id == job.id)))
    if old:
        for version in db.scalars(select(ModelVersion).where(ModelVersion.model_id.in_([m.id for m in old]))):
            db.delete(version)
        for model in old:
            db.delete(model)
        db.flush()
    models = [Model(project_id=job.project_id, job_id=job.id, **record) for record in records]
    db.add_all(models)
    db.flush()
    return models


def export_stats(path: Path, fmt: str) -> dict:
    if fmt == "png":
        from PIL import Image

        with Image.open(path) as image:
            if image.format != "PNG":
                raise ValueError(f"Not a PNG: {path.name}")
            image.verify()
        return {"vertex_count": 0, "face_count": 0, "point_count": 0}

    import struct

    import trimesh

    with path.open("rb") as stream:
        if fmt == "glb":
            magic, version, size = struct.unpack("<4sII", stream.read(12))
            if magic != b"glTF" or version != 2 or size != path.stat().st_size:
                raise ValueError(f"Invalid GLB header: {path.name}")
        elif stream.read(4) not in (b"ply\n", b"ply\r"):
            raise ValueError(f"Invalid PLY header: {path.name}")
        stream.seek(0)
        scene = trimesh.load(stream, file_type=fmt, force="scene", process=False)
    geometries = scene.geometry.values()
    vertices = sum(len(mesh.vertices) for mesh in geometries if hasattr(mesh, "vertices"))
    faces = sum(len(mesh.faces) for mesh in geometries if hasattr(mesh, "faces"))
    points = sum(len(mesh.vertices) for mesh in geometries if isinstance(mesh, trimesh.points.PointCloud))
    return {"vertex_count": vertices, "face_count": faces, "point_count": points}


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
    """Add a version within the caller's transaction."""
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
    return ver


def get_model_checked(db: Session, model_id: int, user_id: int) -> Model:
    m = db.get(Model, model_id)
    if not m:
        raise NotFoundError("模型不存在")
    if m.project.owner_id != user_id:
        raise NotFoundError("模型不存在")
    return m


def job_manifest_path(job: ReconstructionJob) -> str:
    out_dir = project_output_dir(job.project, job.id)
    return str(out_dir / "job_manifest.json")
