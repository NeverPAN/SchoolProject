"""模型 API — 列表/详情/版本/文件/下载/预览。"""
from __future__ import annotations

import json
from math import isfinite
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import ValidationError as SchemaValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_owned_project
from app.core.errors import NotFoundError, ValidationError
from app.db.session import get_db
from app.models.entities import Image, Model, ModelRegion, ModelVersion, Project, User
from app.schemas.api import (JobOut, LocalExtractionOut, LocalReconstructionIn, LocalUpdateIn,
                             ModelDetailOut, ModelOut, ModelRegionIn, ModelRegionOut, ModelVersionOut,
                             ModelVersionOutWithModel)
from app.services import job_service, model_service

router = APIRouter(tags=["models"])

MEDIA_TYPES = {
    "glb": "model/gltf-binary",
    "ply": "application/octet-stream",
    "obj": "text/plain",
    "png": "image/png",
}


@router.get("/projects/{project_id}/models", response_model=list[ModelOut])
def list_models(
    project_id: int,
    db: Session = Depends(get_db),
    project: Project = Depends(get_owned_project),
):
    models = list(db.scalars(select(Model).where(Model.project_id == project.id).order_by(Model.id.desc())))
    return models


def _local_extraction(db: Session, model: Model, manifest: dict) -> LocalExtractionOut | None:
    if (model.stage_origin != "LOCAL_REGION" or model.job_id is None
            or type(manifest.get("job_id")) is not int or manifest["job_id"] != model.job_id
            or manifest.get("pipeline_mode") != "local_update"):
        return None

    params = manifest.get("resolved_params")
    if not isinstance(params, dict):
        return None
    try:
        provenance = LocalExtractionOut.model_validate({
            "parent_model_id": manifest.get("target_model_id"),
            "region_id": manifest.get("region_id"),
            "bounds": {"min": params.get("bbox_min"), "max": params.get("bbox_max")},
        })
    except SchemaValidationError:
        return None

    if provenance.parent_model_id == model.id:
        return None
    parent_id = db.scalar(select(Model.id).where(
        Model.id == provenance.parent_model_id, Model.project_id == model.project_id,
    ))
    return provenance if parent_id is not None else None


@router.get("/models/{model_id}", response_model=ModelDetailOut)
def get_model(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = model_service.get_model_checked(db, model_id, user.id)
    versions = list(db.scalars(select(ModelVersion).where(ModelVersion.model_id == m.id).order_by(ModelVersion.version_number)))
    display = {}
    try:
        manifest = json.loads((Path(m.file_path).parent / "job_manifest.json").read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and manifest.get("job_id") == m.job_id:
            display = manifest
    except (OSError, ValueError, RecursionError):
        pass
    up = None if m.stage_origin == "PHOTO_TEXTURE_UPRIGHT" else display.get("display_up")

    valid_up = (isinstance(up, list) and len(up) == 3
                and all(type(v) in (float, int) and isfinite(v) for v in up)
                and any(up))
    seed = job_service.local_seed(m)
    general_available = seed is None and job_service.general_local_available(m)
    eligible = list(db.scalars(select(Image.id).where(Image.project_id == m.project_id,
                    Image.checksum.in_(list(seed["new_view_sha256"].values()))).order_by(Image.id))) if seed else []
    # 阶段 B：原色贴图可用性以 GLB 实际内容为准（内嵌纹理 + UV），不依赖 stage_origin。
    texture_assets = None
    if m.format == "glb":
        from app.reconstruction.glb_assets import inspect_glb

        texture_assets = inspect_glb(Path(m.file_path))
    return ModelDetailOut(
        local_extraction=_local_extraction(db, m, display),
        local_reconstruction_image_ids=eligible,
        local_reconstruction_available=seed is not None or general_available,
        local_reconstruction_mode="seed" if seed else ("general" if general_available else None),
        local_reconstruction_bounds={"min": seed["bbox_min"], "max": seed["bbox_max"]} if seed else None,
        experiment_name=m.job.experiment_name if m.job else None,
        experiment=display.get("experiment") if m.job and m.job.pipeline_mode == "experiment" else None,
        pipeline_mode=m.job.pipeline_mode if m.job else None,
        **ModelOut.model_validate(m).model_dump(),
        versions=[ModelVersionOut.model_validate(v) for v in versions],
        display_up=tuple(up) if valid_up else None,
        texture_assets=texture_assets,
        legacy_srgb_vertex_colors=(m.model_type == "web" and m.format == "glb"
                                   and Path(m.file_path).name == "model.glb"
                                   and display.get("mesh_glb_color_space") == "srgb"),
    )


@router.get("/models/{model_id}/versions", response_model=list[ModelVersionOutWithModel])
def list_versions(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = model_service.get_model_checked(db, model_id, user.id)
    versions = list(db.scalars(select(ModelVersion).where(ModelVersion.model_id == m.id).order_by(ModelVersion.version_number)))
    return [
        ModelVersionOutWithModel(
            **ModelVersionOut.model_validate(v).model_dump(),
            model=ModelOut.model_validate(m),
        )
        for v in versions
    ]


@router.get("/models/{model_id}/download")
def download_model(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = model_service.get_model_checked(db, model_id, user.id)
    p = Path(m.file_path)
    if not p.is_file():
        raise NotFoundError("模型文件缺失")
    return FileResponse(p, filename=f"model_{m.id}.{m.format}", media_type=MEDIA_TYPES.get(m.format, "application/octet-stream"))


@router.get("/models/{model_id}/file")
def model_file(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查看器数据接口（前端以 Blob+ArrayBuffer 方式带鉴权加载）。"""
    m = model_service.get_model_checked(db, model_id, user.id)
    p = Path(m.file_path)
    if not p.is_file():
        raise NotFoundError("模型文件缺失")
    return FileResponse(p, media_type=MEDIA_TYPES.get(m.format, "application/octet-stream"))


@router.get("/models/{model_id}/preview")
def model_preview(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = model_service.get_model_checked(db, model_id, user.id)
    # 预览图与模型同目录：preview.png（任务级）
    p = Path(m.file_path).parent / "preview.png"
    if not p.is_file():
        raise NotFoundError("预览图不存在")
    return FileResponse(p, media_type="image/png")


@router.get("/models/{model_id}/manifest")
def model_manifest(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = model_service.get_model_checked(db, model_id, user.id)
    p = Path(m.file_path).parent / "job_manifest.json"
    if not p.is_file():
        return {"manifest": None}
    return {"manifest": json.loads(p.read_text(encoding="utf-8"))}


@router.delete("/models/{model_id}")
def delete_model(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = model_service.get_model_checked(db, model_id, user.id)
    from pathlib import Path as _P

    siblings = list(
        db.scalars(select(Model).where(Model.project_id == m.project_id, Model.id != m.id))
    )
    same_job = [s for s in siblings if s.job_id == m.job_id]
    p = _P(m.file_path)
    db.delete(m)
    db.commit()
    # 仅当同目录下已无其他模型文件时删除物理文件
    if p.is_file() and not any(_P(s.file_path) == p for s in same_job):
        p.unlink(missing_ok=True)
    return {"code": "OK", "message": "模型记录已删除", "model_id": model_id}


# ---- 阶段 8 原型：查看器框选区域 + 区域提取任务（非局部重建） ----


def _owned_model(db: Session, model_id: int, user: User) -> Model:
    return model_service.get_model_checked(db, model_id, user.id)


@router.post("/models/{model_id}/regions", response_model=ModelRegionOut, status_code=201)
def create_region(
    model_id: int,
    body: ModelRegionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = _owned_model(db, model_id, user)
    lo = body.bbox_min
    hi = body.bbox_max
    if not all(isfinite(a) and isfinite(b) and a < b for a,b in zip(lo,hi)):
        raise ValidationError("包围盒无效：bbox_max 必须大于 bbox_min")
    region = ModelRegion(
        model_id=m.id, user_id=user.id,
        bbox_min_x=lo[0], bbox_min_y=lo[1], bbox_min_z=lo[2],
        bbox_max_x=hi[0], bbox_max_y=hi[1], bbox_max_z=hi[2],
        camera_json=json.dumps(body.camera, ensure_ascii=False),
        note=body.note,
    )
    db.add(region)
    db.commit()
    db.refresh(region)
    return ModelRegionOut(
        id=region.id, model_id=region.model_id,
        bbox_min=(region.bbox_min_x, region.bbox_min_y, region.bbox_min_z),
        bbox_max=(region.bbox_max_x, region.bbox_max_y, region.bbox_max_z),
        note=region.note, created_at=region.created_at,
    )


@router.get("/models/{model_id}/regions", response_model=list[ModelRegionOut])
def list_regions(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    m = _owned_model(db, model_id, user)
    regions = list(db.scalars(select(ModelRegion).where(ModelRegion.model_id == m.id).order_by(ModelRegion.id.desc())))
    return [
        ModelRegionOut(
            id=r.id, model_id=r.model_id,
            bbox_min=(r.bbox_min_x, r.bbox_min_y, r.bbox_min_z),
            bbox_max=(r.bbox_max_x, r.bbox_max_y, r.bbox_max_z),
            note=r.note, created_at=r.created_at,
        ) for r in regions
    ]


@router.post("/models/{model_id}/local-update", response_model=JobOut, status_code=202)
def create_local_update(
    model_id: int,
    body: LocalUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """阶段 8 原型：为已保存的选区创建区域提取任务（异步，队列执行）。"""
    m = _owned_model(db, model_id, user)
    region = db.get(ModelRegion, body.region_id)
    if region is None or region.model_id != m.id:
        raise NotFoundError("选区不存在")
    job = job_service.create_local_update_job(db, m, body.region_id)
    from app.api.v1.jobs import _job_out

    return _job_out(job)


@router.post("/models/{model_id}/local-reconstruction", response_model=JobOut, status_code=202)
def create_local_reconstruction(
    model_id: int, body: LocalReconstructionIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.api.v1.jobs import _job_out

    model = _owned_model(db, model_id, user)
    region = db.get(ModelRegion, body.region_id)
    if region is None:
        raise NotFoundError("选区不存在")
    return _job_out(job_service.submit_local_reconstruction(db, model, region, body.image_ids))


@router.post("/models/{model_id}/local-mesh", response_model=JobOut, status_code=202)
def create_local_mesh(
    model_id: int, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """阶段 D3/D4：以本局部融合点云候选为源，生成父坐标系的网格补丁候选。"""
    from app.api.v1.jobs import _job_out

    model = _owned_model(db, model_id, user)
    return _job_out(job_service.submit_local_mesh(db, model))
