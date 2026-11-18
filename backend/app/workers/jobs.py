"""RQ Worker task entry and transactional model publication."""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import update

from app.core.config import get_settings
from app.core.logging_setup import get_logger, setup_logging
from app.db.base import utcnow_db
from app.db.session import SessionLocal, create_all
from app.models.entities import JobStageEvent, ReconstructionJob
from app.reconstruction.manifest import atomic_write_json, input_manifest_sha
from app.services import image_service, job_service, model_service

logger = get_logger("worker")


def run_reconstruction_job(job_id: int) -> dict:
    setup_logging(get_settings().log_level)
    create_all()
    with SessionLocal() as db:
        job = db.get(ReconstructionJob, job_id)
        if job is None:
            return {"ok": False, "error": "job not found"}
        work_dir = image_service.project_job_dir(job.project, job_id)
    with job_service.execution_lock(work_dir) as acquired:
        if not acquired:
            return {"ok": False, "error": "job already being executed"}
        return _run_claimed_job(job_id)


def _run_claimed_job(job_id: int) -> dict:
    started = utcnow_db()
    with SessionLocal() as db:
        claimed = db.execute(update(ReconstructionJob).where(
            ReconstructionJob.id == job_id, ReconstructionJob.status.in_(job_service.PENDING),
            ReconstructionJob.cancel_requested.is_(False),
        ).values(status="RUNNING", started_at=started, current_stage=None, progress=0)).rowcount
        if not claimed:
            return {"ok": False, "error": "job not pending"}
        db.commit()

    ctx = None
    try:
        from app.reconstruction.adapter import ColmapAdapter, ColmapNotFoundError, get_colmap_version
        from app.reconstruction.mock_pipeline import MockPipeline
        from app.reconstruction.pipeline import ColmapPipeline, PipelineContext

        with SessionLocal() as db:
            job = db.get(ReconstructionJob, job_id)
            config = json.loads(job.config_json or "{}")
            inputs = json.loads(job.input_manifest or "{}")
            mode = job.pipeline_mode
            ctx = PipelineContext(job, job.project, config, inputs, SessionLocal,
                                  cancel_check=lambda: job_service.is_cancel_requested(job_id))

        ctx.check_cancel("IMAGE_VALIDATION")
        ctx.job_log(f"开始执行任务 {job_id}（mode={mode}, preset={config.get('preset')}）")
        if mode == "mock":
            pipeline = MockPipeline(ctx)
            engine_version = "mock-v2"
        else:
            colmap_bin = get_settings().colmap_bin
            engine_version = get_colmap_version(colmap_bin)
            if not engine_version:
                raise ColmapNotFoundError(f"COLMAP 不可用: {colmap_bin}")
            adapter = ColmapAdapter(colmap_bin, config.get("use_gpu", False), engine_version)
            pipeline = ColmapPipeline(ctx, adapter)

        with SessionLocal() as db:
            db.get(ReconstructionJob, job_id).engine_version = engine_version
            db.commit()

        result = pipeline.run()
        ctx.check_cancel("EXPORTING")
        try:
            with SessionLocal() as db:
                records = model_service.prepare_job_models(db.get(ReconstructionJob, job_id))
        except model_service.InvalidExportError:
            (ctx.checkpoints_dir / "EXPORTING" / "manifest.json").unlink(missing_ok=True)
            raise
        n_models = _publish(job_id, started, records, {
            "job_id": job_id,
            "pipeline_mode": mode,
            "engine_version": engine_version,
            "engine": ctx.engine_identity,
            "config": config,
            # 实际生效的引擎参数（preset→depth 等的解析结果），便于追溯模型由哪组参数生成
            "resolved_params": {
                "max_features": ctx.params.get("max_features"),
                "matcher_default": ctx.params.get("matcher_default"),
                "poisson_depth": ctx.params.get("poisson_depth"),
                "dense_max_image_size": ctx.dense_max_image_size,
            },
            "inputs_sha": input_manifest_sha(inputs),
            "outputs": [item["file_path"] for item in records],
            "display_up": result.get("display_up"),
            "photo_texture": result.get("photo_texture"),
            "mesh_glb_color_space": "linear" if mode == "colmap" else "srgb",
        })
    except Exception as exc:
        stage = getattr(exc, "stage", None) or (ctx.current_stage if ctx else None)
        code = getattr(exc, "code", "PIPELINE_ERROR")
        message = str(exc)
        logger.exception("任务 %s 失败（%s）: %s", job_id, stage, message)
        cancelled = False
        try:
            cancelled = _record_failure(job_id, started, stage, code, message)
        except Exception:
            logger.exception("无法记录任务 %s 终态", job_id)
        if ctx:
            try:
                ctx.job_log(f"任务结束 [{stage}] {'CANCELLED' if cancelled else code}: {message}")
            except OSError:
                logger.warning("任务 %s 日志不可写", job_id)
        if cancelled:
            return {"ok": True, "cancelled": True}
        return {"ok": False, "error": message, "stage": stage}

    warnings = result.get("warnings", [])
    try:
        ctx.job_log(f"任务完成：登记 {n_models} 个模型" + (f"；警告: {'; '.join(warnings)}" if warnings else ""))
    except OSError:
        logger.warning("任务 %s 成功，但日志不可写", job_id)
    return {"ok": True, "models": n_models, "warnings": warnings}


def _publish(job_id: int, started, records: list[dict], manifest: dict) -> int:
    from app.reconstruction.pipeline import StageCancelled

    finished = utcnow_db()
    with SessionLocal() as db:
        # Compare-and-set obtains the write lock before creating any publication rows.
        # Either a cancellation commits first, or this entire publication commits first.
        changed = db.execute(update(ReconstructionJob).where(
            ReconstructionJob.id == job_id, ReconstructionJob.status == "RUNNING",
            ReconstructionJob.started_at == started, ReconstructionJob.cancel_requested.is_(False),
        ).values(status="SUCCEEDED", current_stage=None, progress=100, finished_at=finished)).rowcount
        if not changed:
            raise StageCancelled("EXPORTING")
        job = db.get(ReconstructionJob, job_id)
        models = model_service.register_job_models(db, job, records)
        path = Path(model_service.job_manifest_path(job))
        for model in models:
            model_service.ensure_version(db, model, change_type="initial", description=f"任务 {job_id} 重建结果",
                                         manifest_path=str(path))
        db.flush()
        atomic_write_json(path, {**manifest, "finished_at": finished.isoformat()})
        db.commit()
        return len(models)


def _record_failure(job_id: int, started, stage: str | None, code: str, message: str) -> bool:
    with SessionLocal() as db:
        base = update(ReconstructionJob).where(ReconstructionJob.id == job_id,
                                              ReconstructionJob.started_at == started)
        cancelled = db.execute(base.where(ReconstructionJob.status == "CANCEL_REQUESTED").values(
            status="CANCELLED", cancel_requested=False, error_stage=stage, error_code="CANCELLED",
            error_message="任务已被用户取消", finished_at=utcnow_db(),
        )).rowcount
        if not cancelled:
            changed = db.execute(base.where(ReconstructionJob.status == "RUNNING").values(
                status="CANCELLED" if code == "CANCELLED" else "FAILED", cancel_requested=False,
                error_stage=stage, error_code=code, error_message=message, finished_at=utcnow_db(),
            )).rowcount
            if not changed:
                return False
            cancelled = code == "CANCELLED"
        event_status = "cancelled" if cancelled else "failed"
        had_running = job_service.close_running_events(db, job_id, event_status, message)
        if not had_running and stage:
            now = utcnow_db()
            db.add(JobStageEvent(job_id=job_id, stage=stage, status=event_status,
                                 started_at=now, finished_at=now, duration_ms=0, message=message))
        db.commit()
        return bool(cancelled)
