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
    version_number: int | None = None,
    parent_version_id: int | None = None,
) -> ModelVersion:
    """Add a version within the caller's transaction.

    version_number/parent_version_id 缺省时自动续号；追加照片更新等场景
    由调用方显式传入链上编号与父版本，保证跨模型版本链连续。
    """
    existing = db.scalar(
        select(func.max(ModelVersion.version_number)).where(ModelVersion.model_id == model.id)
    )
    ver = ModelVersion(
        project_id=model.project_id,
        model_id=model.id,
        version_number=version_number if version_number is not None else (existing or 0) + 1,
        parent_version_id=parent_version_id,
        change_type=change_type,
        change_description=description,
        manifest_path=manifest_path,
    )
    db.add(ver)
    return ver


def latest_web_version_before_job(db: Session, project_id: int, job_id: int) -> ModelVersion | None:
    """项目内最近一次（不含本次任务）的 web 模型版本，用于追加照片的版本链。"""
    return db.scalar(
        select(ModelVersion)
        .join(Model, ModelVersion.model_id == Model.id)
        .where(
            Model.project_id == project_id,
            Model.model_type == "web",
            Model.job_id != job_id,
        )
        .order_by(ModelVersion.version_number.desc(), ModelVersion.id.desc())
        .limit(1)
    )


def _input_image_keys(manifest: str | None) -> set[tuple[int, str]] | None:
    """Read just image identity and bytes from a job's frozen input, not the live project."""
    try:
        snapshot = json.loads(manifest) if manifest else None
    except ValueError:
        return None
    if not isinstance(snapshot, dict) or not isinstance(images := snapshot.get("images"), list) or not images:
        return None
    if "image_count" in snapshot and snapshot["image_count"] != len(images):
        return None
    keys = set()
    seen_ids = set()
    for image in images:
        if not isinstance(image, dict):
            return None
        image_id, checksum = image.get("image_id"), image.get("checksum")
        if (type(image_id) is not int or image_id <= 0 or image_id in seen_ids
                or not isinstance(checksum, str) or not checksum.strip()):
            return None
        seen_ids.add(image_id)
        keys.add((image_id, checksum))
    return keys


def describe_job_version_change(job: ReconstructionJob, previous: ReconstructionJob | None) -> tuple[str, str]:
    """Classify a web version against its parent's frozen job input."""
    before = _input_image_keys(previous.input_manifest) if previous else None
    after = _input_image_keys(job.input_manifest)
    prefix = f"任务 {job.id}："
    if before is None or after is None:
        return "inputs_unknown", prefix + "缺少有效输入快照，无法确认照片变化"
    if before == after:
        return "rerun", prefix + "同输入重跑"
    if before < after:
        return "append_images", prefix + f"追加 {len(after - before)} 张照片后更新"
    return "inputs_changed", prefix + "输入照片已删除或替换，重新重建"


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
