"""任务服务 — 创建/取消/恢复/查询（数据库为任务状态权威来源）。"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging_setup import get_logger
from app.db.base import utcnow_db
from app.db.session import SessionLocal
from app.models.entities import (
    Image,
    JobStageEvent,
    Model,
    Project,
    ReconstructionJob,
)
from app.reconstruction.manifest import input_manifest_sha
from app.services import image_service

logger = get_logger("job_service")
ACTIVE = ("CREATED", "VALIDATING", "QUEUED", "RUNNING", "RESUMING", "CANCEL_REQUESTED")
PENDING = ("QUEUED", "RESUMING")
EXECUTING = ("RUNNING", "VALIDATING", "CANCEL_REQUESTED")


@contextmanager
def execution_lock(work_dir: Path):
    """A live worker serializes access to this job's scratch files."""
    import os

    with (work_dir / "execution.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl

            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True  # closing the descriptor releases the lock


def _lock_project(db: Session, project_id: int) -> None:
    # A no-op UPDATE serializes submissions/resumes in both PostgreSQL and SQLite.
    db.execute(update(Project).where(Project.id == project_id)
               .values(updated_at=Project.updated_at).execution_options(synchronize_session=False))


def _other_active(db: Session, project_id: int, exclude_id: int | None = None) -> bool:
    stmt = select(ReconstructionJob.id).where(
        ReconstructionJob.project_id == project_id, ReconstructionJob.status.in_(ACTIVE),
    )
    if exclude_id is not None:
        stmt = stmt.where(ReconstructionJob.id != exclude_id)
    return db.scalar(stmt.limit(1)) is not None


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
    *, status: str = "CREATED",
) -> ReconstructionJob:
    settings = get_settings()
    mode = _resolve_mode(None)
    _lock_project(db, project.id)
    if _other_active(db, project.id):
        raise ConflictError("该项目已有进行中的重建任务，请等待其结束")
    n_images = db.scalar(select(func.count(Image.id)).where(Image.project_id == project.id)) or 0
    if n_images < 3:
        raise ValidationError("项目图片不足 3 张，无法重建")

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
        status=status,
        config_json=json.dumps(config, ensure_ascii=False),
        pipeline_mode=mode,
        input_manifest=json.dumps(manifest, ensure_ascii=False),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def create_local_update_job(db: Session, model: Model, region_id: int) -> ReconstructionJob:
    """阶段 8 原型：为区域提取创建 local_update 任务（进入队列，异步执行）。"""
    _lock_project(db, model.project_id)
    if _other_active(db, model.project_id):
        raise ConflictError("该项目已有进行中的重建任务，请等待其结束")
    config = {"mode": "local_update", "region_id": region_id, "target_model_id": model.id,
              "use_gpu": False, "enable_dense": False, "enable_mesh": False,
              "preset": "standard", "matcher": "auto"}
    job = ReconstructionJob(
        project_id=model.project_id,
        status="QUEUED",
        config_json=json.dumps(config, ensure_ascii=False),
        pipeline_mode="local_update",
        input_manifest="{}",
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
    job = create_job(db, project, preset, matcher, enable_dense, enable_mesh, status="QUEUED")
    _enqueue_or_fail(db, job.id, "QUEUED")
    logger.info("任务 %s 已入队（项目 %s）", job.id, project.id)
    return job


def _enqueue_or_fail(db: Session, job_id: int, pending_status: str) -> None:
    try:
        enqueue_job(job_id)
    except Exception as exc:
        logger.exception("无法入队任务 %s", job_id)
        db.execute(update(ReconstructionJob).where(
            ReconstructionJob.id == job_id, ReconstructionJob.status == pending_status,
        ).values(status="FAILED", error_code="QUEUE_UNAVAILABLE", error_message="任务入队失败",
                 finished_at=utcnow_db()))
        db.commit()
        if db.get(ReconstructionJob, job_id).status == "FAILED":
            raise ConflictError("任务入队失败，请重试", code="QUEUE_UNAVAILABLE") from exc


def is_cancel_requested(job_id: int) -> bool:
    """Worker 侧取消检查（独立短会话）。"""
    with SessionLocal() as db:
        job = db.get(ReconstructionJob, job_id)
        return bool(job and job.cancel_requested)


def request_cancel(db: Session, job: ReconstructionJob) -> ReconstructionJob:
    for _ in range(3):
        status = job.status
        if status in ("RUNNING", "VALIDATING"):
            values = {"status": "CANCEL_REQUESTED", "cancel_requested": True}
        elif status in ("CREATED", *PENDING):
            values = {"status": "CANCELLED", "cancel_requested": False, "finished_at": utcnow_db()}
        else:
            break
        changed = db.execute(update(ReconstructionJob).where(
            ReconstructionJob.id == job.id, ReconstructionJob.status == status,
        ).values(**values)).rowcount
        db.commit()
        db.refresh(job)
        if changed:
            # Leave any already queued delivery alone: a later resume can race
            # dequeue, and the execution claim ignores cancelled deliveries.
            return job
    raise ConflictError(f"当前状态 {job.status} 不允许取消")


def request_resume(db: Session, job: ReconstructionJob) -> ReconstructionJob:
    _lock_project(db, job.project_id)
    db.refresh(job)
    if job.status not in ("FAILED", "CANCELLED"):
        raise ConflictError(f"当前状态 {job.status} 不允许恢复")
    if _other_active(db, job.project_id, exclude_id=job.id):
        raise ConflictError("该项目已有进行中的重建任务，请等待其结束")
    # 校验输入未变化：与当前项目图片清单对比
    current = image_service.build_input_manifest(db, job.project)
    stored = json.loads(job.input_manifest) if job.input_manifest else None
    if stored is None:
        raise ConflictError("任务缺少输入清单，无法安全恢复")
    if input_manifest_sha(current) != input_manifest_sha(stored):
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
    job.current_stage = None
    job.progress = 0.0
    db.commit()
    _enqueue_or_fail(db, job.id, "RESUMING")
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


def close_running_events(db: Session, job_id: int, status: str, message: str) -> bool:
    now = utcnow_db()
    found = False
    for event in db.scalars(select(JobStageEvent).filter_by(job_id=job_id, status="running")):
        found = True
        event.status = status
        event.message = message
        event.finished_at = now
        if event.started_at:
            event.duration_ms = int((now - event.started_at).total_seconds() * 1000)
    return found


def requeue_stuck_jobs() -> int:
    """Reconcile pending deliveries and abandoned executions at worker startup."""
    count = 0
    try:
        import redis
        from rq import Queue
        from rq.job import Job

        conn = redis.Redis.from_url(get_settings().redis_url)
        queue = Queue("recon", connection=conn)
        with SessionLocal() as db:
            rows = db.execute(select(ReconstructionJob.id, ReconstructionJob.status,
                                     ReconstructionJob.started_at).where(
                ReconstructionJob.status.in_(("CREATED", *PENDING, *EXECUTING)))).all()
        registry = queue.started_job_registry
        # Registry members are RQ UUIDs, not application job IDs.
        expired = set(registry.get_expired_job_ids())
        started = Job.fetch_many((rid for rid in registry.get_job_ids(cleanup=False)
                                  if rid not in expired), connection=conn)
        active_ids = {item.args[0] for item in started if item and item.args}
        pending_ids = active_ids | {item.args[0] for item in queue.jobs if item.args}
        for job_id, status, started_at in rows:
            if status not in EXECUTING and job_id in pending_ids:
                continue
            with SessionLocal() as db:
                job = db.get(ReconstructionJob, job_id)
                if job is None or job.status != status or job.started_at != started_at:
                    continue
                work_dir = image_service.project_job_dir(job.project, job_id)
                # Old deployments did not hold this lock while executing.
                if status in EXECUTING and job_id in active_ids and not (work_dir / "execution.lock").exists():
                    continue
                with execution_lock(work_dir) as acquired:
                    if not acquired:
                        continue
                    if status in EXECUTING or status == "CREATED":
                        cancelled = status == "CANCEL_REQUESTED"
                        terminal = "CANCELLED" if cancelled else "FAILED"
                        message = "任务已被用户取消" if cancelled else "Worker 中断，可恢复任务"
                        changed = db.execute(update(ReconstructionJob).where(
                            ReconstructionJob.id == job_id,
                            ReconstructionJob.status == status,
                            ReconstructionJob.started_at == started_at,
                        ).values(status=terminal, error_stage=job.current_stage,
                                 error_code="CANCELLED" if cancelled else "WORKER_LOST",
                                 error_message=message, cancel_requested=False, finished_at=utcnow_db())).rowcount
                        if changed:
                            close_running_events(db, job_id, terminal.lower(), message)
                        db.commit()
                    else:
                        enqueue_job(job_id)
                        count += 1
        if count:
            logger.info("重新入队了 %d 个中断遗留任务", count)
    except Exception:
        logger.exception("无法协调遗留重建任务")
    return count
