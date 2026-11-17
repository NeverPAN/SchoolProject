"""图片 API — 上传/列表/缩略图/删除/质量检查。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_owned_project
from app.core.errors import AppError, NotFoundError, ValidationError
from app.db.session import get_db
from app.models.entities import Image, Project, User
from app.schemas.api import ImageOut, QualityCheckSummary
from app.services import image_service

router = APIRouter(tags=["images"])


@router.post("/projects/{project_id}/images", response_model=list[ImageOut], status_code=201)
def upload_images(
    project_id: int,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    project: Project = Depends(get_owned_project),
):
    """批量上传图片（流式写入；任一文件失败则该文件报错，其余已成功文件保留）。"""
    if len(files) > 200:
        raise ValidationError("单次上传文件数过多（≤200）")
    results: list[ImageOut] = []
    errors: list[dict] = []
    first_app_error: AppError | None = None
    for f in files:
        try:
            img = image_service.save_uploaded_image(db, project, f)
            results.append(_image_out(img))
        except AppError as e:
            errors.append({"filename": f.filename, "error": str(e), "code": e.code})
            if first_app_error is None:
                first_app_error = e
        except Exception as e:
            errors.append({"filename": f.filename, "error": str(e), "code": "INTERNAL_ERROR"})
    if errors and not results:
        # 全部失败：保持原始错误类型/码（如 409 内容重复、400 格式不支持）
        if first_app_error is not None:
            raise first_app_error
        raise ValidationError(f"全部上传失败: {json.dumps(errors, ensure_ascii=False)}")
    if errors:
        # 部分成功：附加错误信息到响应头（JSON body 只返回成功项）
        for r in results:
            _ = r
        return _partial_response(results, errors)
    return results


def _partial_response(results, errors):
    from fastapi.responses import JSONResponse

    payload = {
        "partial_errors": errors,
        "items": [json.loads(r.model_dump_json()) for r in results],
    }
    return JSONResponse(status_code=201, content=payload)


def _image_out(img: Image) -> ImageOut:
    return ImageOut(
        id=img.id,
        project_id=img.project_id,
        original_filename=img.original_filename,
        stored_name=Path(img.stored_path).name,
        file_size=img.file_size,
        width=img.width,
        height=img.height,
        format=img.format,
        quality_status=img.quality_status,
        has_thumbnail=bool(img.thumbnail_path and Path(img.thumbnail_path).is_file()),
        checksum=img.checksum,
        exif=json.loads(img.exif_json) if img.exif_json else None,
        quality_notes=img.quality_notes,
        created_at=img.created_at,
    )


@router.get("/projects/{project_id}/images", response_model=list[ImageOut])
def list_images(
    project_id: int,
    db: Session = Depends(get_db),
    project: Project = Depends(get_owned_project),
):
    images = list(db.scalars(select(Image).where(Image.project_id == project.id).order_by(Image.id)))
    return [_image_out(i) for i in images]


@router.get("/images/{image_id}/thumbnail")
def image_thumbnail(
    image_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    img = db.get(Image, image_id)
    if not img or img.project.owner_id != user.id:
        raise NotFoundError("图片不存在")
    if not img.thumbnail_path or not Path(img.thumbnail_path).is_file():
        raise NotFoundError("缩略图不存在")
    return FileResponse(img.thumbnail_path, media_type="image/jpeg")


@router.get("/images/{image_id}/file")
def image_file(
    image_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    img = db.get(Image, image_id)
    if not img or img.project.owner_id != user.id:
        raise NotFoundError("图片不存在")
    return FileResponse(img.stored_path, filename=img.original_filename)


@router.delete("/images/{image_id}")
def delete_image(
    image_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    img = db.get(Image, image_id)
    if not img or img.project.owner_id != user.id:
        raise NotFoundError("图片不存在")
    image_service.delete_image(db, img)
    return {"code": "OK", "message": "图片已删除", "image_id": image_id}


@router.post("/projects/{project_id}/quality-check", response_model=QualityCheckSummary)
def quality_check(
    project_id: int,
    db: Session = Depends(get_db),
    project: Project = Depends(get_owned_project),
):
    summary = image_service.run_quality_check(db, project)
    return QualityCheckSummary(**summary)


@router.get("/projects/{project_id}/images/{image_id}/exif")
def image_exif(
    project_id: int,
    image_id: int,
    db: Session = Depends(get_db),
    project: Project = Depends(get_owned_project),
):
    img = db.get(Image, image_id)
    if not img or img.project_id != project.id:
        raise NotFoundError("图片不存在")
    return {"image_id": img.id, "exif": json.loads(img.exif_json) if img.exif_json else {}, "notes": img.quality_notes}
