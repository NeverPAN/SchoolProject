"""RQ Worker 任务函数 — 在 Worker 进程中执行重建。"""
from __future__ import annotations

import json
import traceback
from app.db.base import utcnow_db

from app.core.config import get_settings
from app.core.logging_setup import get_logger, setup_logging
from app.db.session import SessionLocal, create_all
from app.models.entities import Model, ModelVersion, ReconstructionJob
from app.reconstruction.manifest import compute_sha_of
from app.services import job_service, model_service

logger = get_logger("worker")


def run_reconstruction_job(job_id: int) -> dict:
    """Worker 入口：执行阶段化管线，维护任务状态机。"""
    setup_logging(get_settings().log_level)
    create_all()
    with SessionLocal() as db:
        job = db.get(ReconstructionJob, job_id)
        if job is None:
            logger.error("任务 %s 不存在", job_id)
            return {"ok": False, "error": "job not found"}
        if job.status not in ("QUEUED", "RESUMING"):
            logger.warning("任务 %s 状态为 %s，跳过执行", job_id, job.status)
            return {"ok": False, "error": f"invalid status {job.status}"}
        if job.cancel_requested:
            job.status = "CANCELLED"
            job.finished_at = utcnow_db()
            db.commit()
            return {"ok": True, "cancelled": True}

        project = job.project
        config = json.loads(job.config_json or "{}")
        input_manifest = json.loads(job.input_manifest or "{}")
        mode = job.pipeline_mode
        job.status = "RUNNING"
        job.started_at = utcnow_db()
        db.commit()

    ctx = None
    try:
        from app.reconstruction.pipeline import ColmapPipeline, PipelineContext
        from app.reconstruction.mock_pipeline import MockPipeline

        with SessionLocal() as db:
            job = db.get(ReconstructionJob, job_id)
            project = job.project
            ctx = PipelineContext(
                job=job,
                project=project,
                config=config,
                input_manifest=input_manifest,
                session_factory=SessionLocal,
                cancel_check=lambda: job_service.is_cancel_requested(job_id),
            )

        ctx.job_log(f"开始执行任务 {job_id}（mode={mode}, preset={config.get('preset')}）")

        if mode == "mock":
            pipeline = MockPipeline(ctx)
            engine_version = "mock-v1"
        else:
            from app.reconstruction.adapter import ColmapAdapter, get_colmap_version

            settings = get_settings()
            engine_version = get_colmap_version(settings.colmap_bin) or "unknown"
            adapter = ColmapAdapter(settings.colmap_bin, config.get("use_gpu", False), engine_version)
            pipeline = ColmapPipeline(ctx, adapter)

        with SessionLocal() as db:
            j = db.get(ReconstructionJob, job_id)
            j.engine_version = engine_version
            db.commit()

        result = pipeline.run()

        # 模型登记 + 版本
        with SessionLocal() as db:
            j = db.get(ReconstructionJob, job_id)
            models = model_service.register_job_models(db, j)
            for m in models:
                model_service.ensure_version(db, m, change_type="initial", description=f"任务 {job_id} 重建结果")
            # 写任务级 manifest（输出+输入+配置）
            from app.services.image_service import project_output_dir

            out_dir = project_output_dir(j.project, j.id)
            manifest = {
                "job_id": job_id,
                "pipeline_mode": mode,
                "engine_version": engine_version,
                "config": config,
                "inputs_sha": compute_sha_of(input_manifest),
                "outputs": [m.file_path for m in models],
                "finished_at": utcnow_db().isoformat(),
            }
            (out_dir / "job_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            j.status = "SUCCEEDED"
            j.current_stage = None
            j.progress = 100.0
            j.finished_at = utcnow_db()
            db.commit()
            n_models = len(models)

        warnings = result.get("warnings", [])
        ctx.job_log(f"任务完成：登记 {n_models} 个模型" + (f"；警告: {'; '.join(warnings)}" if warnings else ""))
        return {"ok": True, "models": n_models, "warnings": warnings}

    except Exception as e:
        # 统一失败处理（自身不允许再抛异常，否则任务会卡在 RUNNING）
        from app.reconstruction.pipeline import PipelineError

        stage = getattr(e, "stage", None) or (getattr(ctx, "current_stage", None) if ctx else None)
        code = getattr(e, "code", None) or "PIPELINE_ERROR"
        message = str(e)
        if not isinstance(e, PipelineError):
            message = f"{type(e).__name__}: {e}"
        logger.error("任务 %s 失败（%s）: %s\n%s", job_id, stage, message, traceback.format_exc())
        cancelled = False
        try:
            with SessionLocal() as db:
                j = db.get(ReconstructionJob, job_id)
                cancelled = bool(j.cancel_requested)
        except Exception as inner:
            logger.error("读取取消标记时出错: %s", inner)
        try:
            if ctx is not None:
                stage_status = "cancelled" if (cancelled or code == "CANCELLED") else "failed"
                ctx.job_log(f"任务结束 [{stage}] {stage_status}: {message}")
                ctx.set_stage_result(stage or "UNKNOWN", stage_status, message)
        except Exception as inner:
            logger.error("记录阶段失败状态时出错: %s", inner)
        try:
            with SessionLocal() as db:
                j = db.get(ReconstructionJob, job_id)
                j.status = "CANCELLED" if (cancelled or code == "CANCELLED") else "FAILED"
                j.cancel_requested = False
                j.error_stage = stage
                j.error_code = code if not cancelled else "CANCELLED"
                j.error_message = message if not cancelled else "任务已被用户取消"
                j.finished_at = utcnow_db()
                db.commit()
        except Exception as inner:
            logger.error("更新任务失败状态时出错: %s", inner)
        return {"ok": False, "error": message, "stage": stage}
