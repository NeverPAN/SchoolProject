"""任务 API — 创建/状态/事件/日志/取消/恢复。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_owned_project
from app.db.session import get_db
from app.models.entities import JobStageEvent, Project, ReconstructionJob, User
from app.schemas.api import JobConfigOut, JobCreateRequest, JobDetailOut, JobOut, JobStageEventOut
from app.services import job_service

router = APIRouter(tags=["jobs"])


def _job_out(job: ReconstructionJob) -> JobOut:
    return JobOut(
        id=job.id,
        project_id=job.project_id,
        status=job.status,
        current_stage=job.current_stage,
        progress=job.progress,
        pipeline_mode=job.pipeline_mode,
        experiment_name=job.experiment_name,
        engine_version=job.engine_version,
        config=JobConfigOut(**json.loads(job.config_json or "{}")),
        started_at=job.started_at,
        finished_at=job.finished_at,
        error_stage=job.error_stage,
        error_code=job.error_code,
        error_message=job.error_message,
        created_at=job.created_at,
    )


@router.post("/projects/{project_id}/jobs", response_model=JobOut, status_code=202)
def create_job(
    project_id: int,
    body: JobCreateRequest,
    db: Session = Depends(get_db),
    project: Project = Depends(get_owned_project),
):
    """创建并提交重建任务（异步：立即返回 202）。"""
    job = job_service.submit_new_job(
        db, project, body.preset, body.matcher, body.enable_dense, body.enable_mesh,
        experiment_name=body.experiment_name,
    )
    return _job_out(job)


@router.get("/projects/{project_id}/jobs", response_model=list[JobOut])
def list_jobs(
    project_id: int,
    db: Session = Depends(get_db),
    project: Project = Depends(get_owned_project),
):
    jobs = list(
        db.scalars(
            select(ReconstructionJob).where(ReconstructionJob.project_id == project.id).order_by(ReconstructionJob.id.desc())
        )
    )
    return [_job_out(j) for j in jobs]


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = job_service.get_job_checked(db, job_id, user.id)
    events = list(db.scalars(select(JobStageEvent).where(JobStageEvent.job_id == job.id).order_by(JobStageEvent.id)))
    out = _job_out(job)
    return JobDetailOut(
        **out.model_dump(),
        events=[
            {
                "id": e.id,
                "stage": e.stage,
                "status": e.status,
                "started_at": e.started_at,
                "finished_at": e.finished_at,
                "duration_ms": e.duration_ms,
                "message": e.message,
            }
            for e in events
        ],
    )


@router.get("/jobs/{job_id}/events", response_model=list[JobStageEventOut])
def get_job_events(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = job_service.get_job_checked(db, job_id, user.id)
    events = list(db.scalars(select(JobStageEvent).where(JobStageEvent.job_id == job.id).order_by(JobStageEvent.id)))
    return events


@router.get("/jobs/{job_id}/logs", response_class=PlainTextResponse)
def get_job_logs(
    job_id: int,
    stage: str | None = Query(default=None),
    tail: int = Query(default=400, le=5000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = job_service.get_job_checked(db, job_id, user.id)
    if stage:
        return job_service.read_stage_log(job, stage, tail)
    return job_service.read_job_log(job, tail)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = job_service.get_job_checked(db, job_id, user.id)
    job = job_service.request_cancel(db, job)
    return {"code": "OK", "message": "取消请求已提交", "job_id": job.id, "status": job.status}


@router.post("/jobs/{job_id}/resume")
def resume_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = job_service.get_job_checked(db, job_id, user.id)
    job = job_service.request_resume(db, job)
    return {"code": "OK", "message": "任务已重新入队（将从有效检查点恢复）", "job_id": job.id, "status": job.status}
