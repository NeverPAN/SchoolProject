"""任务服务 — 创建/取消/恢复/查询（数据库为任务状态权威来源）。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging_setup import get_logger
from app.db.base import utcnow_db
from app.db.session import SessionLocal
from app.models.entities import (
    Image,
    JobStageEvent,
    Model,
    ReconstructionJob,
)
from app.services import image_service, model_service

logger = get_logger("job_service")


def _resolve_mode(requested: str | None) -> str:
    settings = get_settings()
    mode = requested or settings.recon_mode
    if mode not in ("colmap", "mock"):
        raise ValidationError(f"未知的重建模式 {mode}")
    if mode == "colmap":
        from app.reconstruction.adapter import get_colmap_version

        if get_colmap_version(settings.colmap_bin) is None:
            raise ValidationError(
                "COLMAP 不可用（未安装或路径错误），可在 .env 设置 RECON_MODE=mock 使用模拟管线",
                code="COLMAP_UNAVAILABLE",
            )
    return mode


def create_job(
    db: Session,
    project,
    preset: str,
    matcher: str,
    enable_dense: bool,
    enable_mesh: bool,
) -> ReconstructionJob:
    settings = get_settings()
    # 已有运行中任务的项目：拒绝并发重建（单 Worker 串行 + 项目级互斥）
    running = db.scalar(
        select(func.count(ReconstructionJob.id)).where(
            ReconstructionJob.project_id == project.id,
            ReconstructionJob.status.in_(["CREATED", "VALIDATING", "QUEUED", "RUNNING", "RESUMING", "CANCEL_REQUESTED"]),
        )
    )
    if running:
        raise ConflictError("该项目已有进行中的重建任务，请等待其结束")
    n_images = db.scalar(select(func.count(Image.id)).where(Image.project_id == project.id)) or 0
    if n_images < 3:
        raise ValidationError("项目图片不足 3 张，无法重建")

    mode = _resolve_mode(None)
    config = {
        "preset": preset,
        "matcher": matcher,
        "enable_dense": bool(enable_dense),
        "enable_mesh": bool(enable_mesh),
        "use_gpu": settings.use_gpu if mode == "colmap" else False,
        "mode": mode,
    }
    manifest = image_service.build_input_manifest(db, project)
    job = ReconstructionJob(
        project_id=project.id,
        status="CREATED",
        config_json=json.dumps(config, ensure_ascii=False),
        pipeline_mode=mode,
        input_manifest=json.dumps(manifest, ensure_ascii=False),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def enqueue_job(job_id: int) -> None:
    """将任务写入 Redis 队列（RQ）。"""
    settings = get_settings()
    import redis
    from rq import Queue

    conn = redis.Redis.from_url(settings.redis_url)
    queue = Queue("recon", connection=conn)
    queue.enqueue("app.workers.jobs.run_reconstruction_job", job_id, job_timeout=24 * 3600)


def submit_new_job(db: Session, project, preset: str, matcher: str, enable_dense: bool, enable_mesh: bool) -> ReconstructionJob:
    job = create_job(db, project, preset, matcher, enable_dense, enable_mesh)
    job.status = "QUEUED"
    db.commit()
    enqueue_job(job.id)
    logger.info("任务 %s 已入队（项目 %s）", job.id, project.id)
    return job


def is_cancel_requested(job_id: int) -> bool:
    """Worker 侧取消检查（独立短会话）。"""
    with SessionLocal() as db:
        job = db.get(ReconstructionJob, job_id)
        return bool(job and job.cancel_requested)


def request_cancel(db: Session, job: ReconstructionJob) -> ReconstructionJob:
    if job.status in ("RUNNING", "VALIDATING", "RESUMING"):
        job.cancel_requested = True
        job.status = "CANCEL_REQUESTED"
        db.commit()
        return job
    if job.status in ("CREATED", "QUEUED"):
        # 未开始执行：直接标记取消
        job.status = "CANCELLED"
        job.cancel_requested = False
        job.finished_at = utcnow_db()
        db.commit()
        _dequeue_if_pending(job.id)
        return job
    raise ConflictError(f"当前状态 {job.status} 不允许取消")


def _dequeue_if_pending(job_id: int) -> None:
    """尝试把仍在队列中未执行的 Job 移除（已在执行则由 Worker 响应取消）。"""
    try:
        settings = get_settings()
        import redis
        from rq import Queue

        conn = redis.Redis.from_url(settings.redis_url)
        q = Queue("recon", connection=conn)
        for j in q.jobs:
            if j.args and j.args[0] == job_id:
                q.remove(j)
    except Exception as e:
        logger.warning("移除队列任务失败（可忽略）: %s", e)


def request_resume(db: Session, job: ReconstructionJob) -> ReconstructionJob:
    if job.status not in ("FAILED", "CANCELLED"):
        raise ConflictError(f"当前状态 {job.status} 不允许恢复")
    if job.pipeline_mode != "colmap":
        pass  # mock 同样支持断点恢复
    # 校验输入未变化：与当前项目图片清单对比
    current = image_service.build_input_manifest(db, job.project)
    stored = json.loads(job.input_manifest) if job.input_manifest else None
    if stored is None:
        raise ConflictError("任务缺少输入清单，无法安全恢复")
    if [e["checksum"] for e in current["images"]] != [e["checksum"] for e in stored["images"]]:
        raise ConflictError(
            "项目图片相对该任务已发生变化，不允许恢复旧任务；请创建新任务",
            code="INPUT_CHANGED",
        )
    job.cancel_requested = False
    job.status = "RESUMING"
    job.error_stage = None
    job.error_code = None
    job.error_message = None
    job.finished_at = None
    db.commit()
    enqueue_job(job.id)
    logger.info("任务 %s 请求恢复并重新入队", job.id)
    return job


def get_job_checked(db: Session, job_id: int, user_id: int) -> ReconstructionJob:
    job = db.get(ReconstructionJob, job_id)
    if not job:
        raise NotFoundError("任务不存在")
    if job.project.owner_id != user_id:
        raise NotFoundError("任务不存在")
    return job


def read_job_log(job: ReconstructionJob, tail_lines: int = 400) -> str:
    from pathlib import Path

    from app.services.image_service import project_job_dir

    log_path = Path(project_job_dir(job.project, job.id)) / "logs" / "job.log"
    if not log_path.is_file():
        return ""
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-tail_lines:])


def read_stage_log(job: ReconstructionJob, stage: str, tail_lines: int = 400) -> str:
    from pathlib import Path

    from app.services.image_service import project_job_dir

    log_path = Path(project_job_dir(job.project, job.id)) / "logs" / f"stage-{stage.lower()}.log"
    if not log_path.is_file():
        return ""
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-tail_lines:])


def requeue_stuck_jobs() -> int:
    """Worker 启动时调用：把数据库里 QUEUED/RESUMING 但 Redis 无记录的任务重新入队。"""
    try:
        settings = get_settings()
        import redis
        from rq import Queue
        from rq.job import Job

        conn = redis.Redis.from_url(settings.redis_url)
        q = Queue("recon", connection=conn)
        queued_ids = set()
        for j in q.jobs:
            if j.args:
                queued_ids.add(j.args[0])
        # 正在执行的由 StartedJobRegistry 表示
        started = set()
        try:
            started = {int(j.get_id()) for j in q.started_job_registry.get_job_ids()} if hasattr(q.started_job_registry, "get_job_ids") else set()
            from rq.job import Job as _J
            started = set()
            for jid in q.started_job_registry.get_job_ids():
                started.add(int(jid.decode() if isinstance(jid, bytes) else jid))
        except Exception:
            pass
        count = 0
        with SessionLocal() as db:
            rows = db.scalars(
                select(ReconstructionJob).where(
                    ReconstructionJob.status.in_(["QUEUED", "RESUMING"])
                )
            )
            for job in rows:
                if job.id in queued_ids or job.id in started:
                    continue
                enqueue_job(job.id)
                count += 1
        if count:
            logger.info("重新入队了 %d 个中断遗留任务", count)
        return count
    except Exception as e:
        logger.warning("requeue_stuck_jobs 失败: %s", e)
        return 0
