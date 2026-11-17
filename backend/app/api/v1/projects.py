"""项目 API — CRUD + 统计。"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_owned_project
from app.core.errors import NotFoundError
from app.db.session import get_db
from app.models.entities import Image, Model, Project, ReconstructionJob, User
from app.schemas.api import ProjectCreate, ProjectDetailOut, ProjectOut, ProjectUpdate
from app.services import image_service

router = APIRouter(prefix="/projects", tags=["projects"])


def _project_out(db: Session, p: Project) -> ProjectOut:
    stats = image_service.project_stats(db, p)
    model_count = db.scalar(select(func.count(Model.id)).where(Model.project_id == p.id)) or 0
    job_count = db.scalar(select(func.count(ReconstructionJob.id)).where(ReconstructionJob.project_id == p.id)) or 0
    return ProjectOut(
        id=p.id,
        name=p.name,
        description=p.description,
        status=p.status,
        owner_id=p.owner_id,
        created_at=p.created_at,
        updated_at=p.updated_at,
        image_count=stats["image_count"],
        total_image_bytes=stats["total_image_bytes"],
        model_count=int(model_count),
        job_count=int(job_count),
    )


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    projects = list(db.scalars(select(Project).where(Project.owner_id == user.id).order_by(Project.id.desc())))
    return [_project_out(db, p) for p in projects]


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = Project(owner_id=user.id, name=body.name, description=body.description)
    db.add(p)
    db.commit()
    db.refresh(p)
    return _project_out(db, p)


@router.get("/{project_id}", response_model=ProjectDetailOut)
def get_project(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = db.get(Project, project_id)
    if not p or p.owner_id != user.id:
        raise NotFoundError("项目不存在")
    base = _project_out(db, p)
    images = list(db.scalars(select(Image).where(Image.project_id == p.id).order_by(Image.id)))
    stats = image_service.project_stats(db, p)
    return ProjectDetailOut(
        **base.model_dump(),
        images=[
            {
                "id": im.id,
                "original_filename": im.original_filename,
                "file_size": im.file_size,
                "width": im.width,
                "height": im.height,
                "format": im.format,
                "quality_status": im.quality_status,
                "has_thumbnail": bool(im.thumbnail_path and Path(im.thumbnail_path).is_file()),
            }
            for im in images
        ],
        quality_summary=stats["quality_summary"],
    )


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    body: ProjectUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = db.get(Project, project_id)
    if not p or p.owner_id != user.id:
        raise NotFoundError("项目不存在")
    if body.name is not None:
        p.name = body.name
    if body.description is not None:
        p.description = body.description
    p.updated_at = datetime.now(timezone.utc)
    db.commit()
    return _project_out(db, p)


@router.delete("/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = db.get(Project, project_id)
    if not p or p.owner_id != user.id:
        raise NotFoundError("项目不存在")
    # 运行中任务不允许删除项目
    running = db.scalar(
        select(func.count(ReconstructionJob.id)).where(
            ReconstructionJob.project_id == p.id,
            ReconstructionJob.status.in_(["CREATED", "QUEUED", "RUNNING", "RESUMING", "CANCEL_REQUESTED", "VALIDATING"]),
        )
    )
    if running:
        from app.core.errors import ConflictError

        raise ConflictError("项目存在进行中的重建任务，请先取消或等待结束")
    # 清理文件目录（图片/缩略图/输出/日志），路径按约定规则推导
    from app.core.config import get_settings

    settings = get_settings()
    data_project_root = (
        Path(settings.data_dir) / "users" / str(p.owner_id) / "projects" / str(p.id)
    )
    scratch_project_root = (
        Path(settings.scratch_dir) / "users" / str(p.owner_id) / "projects" / str(p.id)
    )
    db.delete(p)
    db.commit()
    shutil.rmtree(data_project_root, ignore_errors=True)
    shutil.rmtree(scratch_project_root, ignore_errors=True)
    return {"code": "OK", "message": "项目已删除", "project_id": project_id}
