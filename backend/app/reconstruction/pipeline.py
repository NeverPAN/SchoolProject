"""重建管线 — 阶段化执行、检查点断点续跑、取消、模型登记。

设计要点（指南 B2 / 阶段5）：
- 每个阶段输出写入独立目录，完成后写 manifest（参数哈希 + 输入哈希 + 文件校验和）；
- 恢复时校验检查点（参数/输入一致 + 文件完整）才允许跳过；
- 取消在阶段边界与子进程运行期间均可响应（进程组 SIGTERM）；
- 数据库是任务状态权威来源；进度为"阶段权重估算值"。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.logging_setup import get_logger
from app.models.entities import (
    PIPELINE_STAGES,
    JobStageEvent,
    ReconstructionJob,
)
from app.reconstruction import export as export_tools
from app.reconstruction.manifest import (
    atomic_write_json,
    checked_manifest,
    checkpoint_fingerprint,
    compute_sha_of,
    input_manifest_sha,
    sha256_file,
    write_manifest,
)
from app.utils.files import human_size, sanitize_filename

logger = get_logger("pipeline")

PRESET_PARAMS = {
    # poisson_depth 与开发指南的预设表一致（fast=8 / standard=10 / high=12）。
    # 第二轮在正式 Bridge 数据上的 depth×trim 对照表明：同一份 2000px 稠密点云，
    # depth 10 的点支撑面积 74.3%、无支撑外插 11.1%，显著优于 depth 8 的 65.7%/27.8%。
    # high 的 depth 12 网格量级可达数百万面，贴图与查看器开销大，未在本轮验收。
    "fast": {"max_features": 4096, "poisson_depth": 8, "matcher_default": "sequential"},
    "standard": {"max_features": 8192, "poisson_depth": 10, "matcher_default": "exhaustive"},
    "high": {"max_features": 16384, "poisson_depth": 12, "matcher_default": "exhaustive"},
}


POISSON_THREADS = 4
SPARSE_MODEL_FILES = ("cameras.bin", "images.bin", "points3D.bin")


class PipelineError(Exception):
    def __init__(self, stage: str, code: str, message: str):
        self.stage = stage
        self.code = code
        self.message = message
        super().__init__(f"[{stage}] {code}: {message}")


class StageCancelled(PipelineError):
    def __init__(self, stage: str):
        super().__init__(stage, "CANCELLED", "任务已被用户取消")


class PipelineContext:
    """单个任务的一次执行上下文。"""

    def __init__(
        self,
        job: ReconstructionJob,
        project,
        config: dict,
        input_manifest: dict,
        session_factory: sessionmaker,
        cancel_check: Callable[[], bool] | None = None,
    ):
        self.job_id = job.id
        self.project_id = project.id
        self.owner_id = project.owner_id
        self.config = config
        self.input_manifest = input_manifest
        self.session_factory = session_factory
        self.cancel_check = cancel_check or (lambda: False)

        from app.services.image_service import project_job_dir, project_output_dir

        self.work_dir = project_job_dir(project, job.id).resolve()
        self.out_dir = project_output_dir(project, job.id).resolve()
        self.logs_dir = self.work_dir / "logs"
        self.checkpoints_dir = self.work_dir / "checkpoints"
        for d in (self.work_dir, self.out_dir, self.logs_dir, self.checkpoints_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.params = PRESET_PARAMS.get(config.get("preset", "standard"), PRESET_PARAMS["standard"])
        self.current_stage: str | None = None  # 供异常处理读取（不依赖 DB）
        # 恢复判定输入：任务级（输入清单 + 配置）
        self.job_inputs_sha = compute_sha_of(
            {"images_sha": input_manifest_sha(input_manifest), "config": config}
        )
        self.engine_identity: dict = {"mode": job.pipeline_mode, "version": job.engine_version}
        self.dense_max_image_size = get_settings().colmap_dense_max_image_size
        self.stage_fingerprints: dict[str, str] = {}
        self.completed_stages: set[str] = set()

    def log_file(self, stage: str) -> Path:
        return self.logs_dir / f"stage-{stage.lower()}.log"

    def job_log(self, message: str) -> None:
        with open(self.logs_dir / "job.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    # ---- DB 更新（短会话，Worker 内使用）----

    def _db(self) -> Session:
        return self.session_factory()

    def set_stage_running(self, stage: str) -> None:
        self.current_stage = stage
        with self._db() as db:
            changed = db.query(ReconstructionJob).filter_by(
                id=self.job_id, status="RUNNING", cancel_requested=False,
            ).update({"current_stage": stage})
            if not changed:
                raise StageCancelled(stage)
            db.add(JobStageEvent(job_id=self.job_id, stage=stage, status="running", started_at=_now()))
            db.commit()

    def set_stage_result(self, stage: str, status: str, message: str | None = None) -> None:
        with self._db() as db:
            job = db.get(ReconstructionJob, self.job_id)
            ev = (
                db.query(JobStageEvent)
                .filter(JobStageEvent.job_id == job.id, JobStageEvent.stage == stage, JobStageEvent.status == "running")
                .order_by(JobStageEvent.id.desc())
                .first()
            )
            now = _now()
            if ev:
                ev.status = status
                ev.finished_at = now
                if ev.started_at:
                    ev.duration_ms = int((now - ev.started_at).total_seconds() * 1000)
                ev.message = message
            else:
                # 无对应 running 事件（如断点恢复时的 skipped）：补建事件记录
                db.add(
                    JobStageEvent(
                        job_id=job.id, stage=stage, status=status,
                        started_at=now, finished_at=now, duration_ms=0, message=message,
                    )
                )
            # 进度 = 已完成（含跳过）阶段权重之和
            if status in ("completed", "skipped"):
                weights = dict(PIPELINE_STAGES)
                self.completed_stages.add(stage)
                job.progress = float(sum(weights.get(s, 0) for s in self.completed_stages))
            db.commit()

    def check_cancel(self, stage: str) -> None:
        if self.cancel_check():
            raise StageCancelled(stage)

    def stage_params(self, stage: str) -> dict:
        """阶段参数（进入检查点哈希；变化即失效旧检查点）。"""
        return {
            "preset": self.config.get("preset", "standard"),
            "matcher": self.config.get("matcher", "auto"),
            "enable_dense": bool(self.config.get("enable_dense", True)),
            "enable_mesh": bool(self.config.get("enable_mesh", True)),
            "mode": self.config.get("mode", "colmap"),
            "stage": stage,
            "engine": self.engine_identity,
            **({"max_image_size": self.dense_max_image_size}
               if stage in ("DENSE_RECONSTRUCTION", "STEREO_FUSION") else {}),
            **({"poisson_depth": self.params["poisson_depth"], "poisson_threads": POISSON_THREADS}
               if stage == "MESHING" else {}),
            **({"max_features": self.params["max_features"]} if stage == "FEATURE_EXTRACTION" else {}),
        }

    def stage_inputs(self, stage: str, upstream=()) -> dict:
        return {"job_inputs_sha": self.job_inputs_sha,
                "upstream": {name: self.stage_fingerprints[name] for name in upstream}}

    def write_stage_manifest(self, stage: str, stage_dir: Path, files, upstream=()) -> dict:
        self.check_cancel(stage)
        manifest = write_manifest(stage_dir, stage, self.stage_params(stage), self.stage_inputs(stage, upstream), files)
        self.stage_fingerprints[stage] = checkpoint_fingerprint(manifest)
        return manifest

    def try_resume_stage(self, stage: str, stage_dir: Path, required_hint, upstream=()) -> bool:
        self.check_cancel(stage)
        manifest = checked_manifest(stage_dir, stage, self.stage_params(stage),
                                    self.stage_inputs(stage, upstream), required_hint)
        if manifest is None:
            (stage_dir / "manifest.json").unlink(missing_ok=True)
            self.stage_fingerprints.pop(stage, None)
            return False
        self.stage_fingerprints[stage] = checkpoint_fingerprint(manifest)
        self.set_stage_result(stage, "skipped", message="检查点有效，跳过（断点恢复）")
        self.job_log(f"[{stage}] 检查点有效，跳过")
        return True


def _now():
    from app.db.base import utcnow_db

    return utcnow_db()


# ---------------------------------------------------------------------------
# COLMAP 管线
# ---------------------------------------------------------------------------


class ColmapPipeline:
    def __init__(self, ctx: PipelineContext, adapter):
        self.ctx = ctx
        self.adapter = adapter
        ctx.engine_identity = getattr(adapter, "engine_identity", {
            "mode": "colmap", "version": adapter.version, "use_gpu": adapter.use_gpu,
        })
        self.has_cuda = adapter.use_gpu
        self.enable_dense = bool(ctx.config.get("enable_dense", True)) and self.has_cuda
        self.enable_mesh = bool(ctx.config.get("enable_mesh", True))
        # 适配层要求无参 cancel_check；取消时以当前阶段名抛出 StageCancelled
        self._cc = lambda: ctx.check_cancel(ctx.current_stage or "?")

    def run(self) -> dict:
        ctx = self.ctx
        work = ctx.work_dir / "work"
        work.mkdir(parents=True, exist_ok=True)
        result: dict = {"skipped_stages": [], "warnings": []}

        self._stage_image_validation()
        self._stage_feature_extraction(work)
        model_dir = self._stage_sparse(work)
        from app.reconstruction.orientation import estimate_display_up

        result["display_up"] = estimate_display_up(model_dir / "images.bin")
        dense_dir = work / "dense"
        fused_ply: Path | None = None
        if self.enable_dense:
            self._stage_undistort(work, model_dir, dense_dir)
            self._stage_dense(dense_dir)
            fused_ply = self._stage_fusion(dense_dir)
        else:
            if not self.has_cuda and ctx.config.get("enable_dense", True):
                result["warnings"].append(
                    "当前 COLMAP 为 CPU 构建（无 CUDA），patch_match_stereo 仅支持 GPU，已跳过稠密重建；"
                    "如需稠密模型请安装 CUDA 版 COLMAP（见 docs/environment-report.md）"
                )
            ctx.set_stage_result("IMAGE_UNDISTORTION", "skipped", "未启用稠密重建（或无 CUDA）")
            ctx.set_stage_result("DENSE_RECONSTRUCTION", "skipped", "未启用稠密重建（或无 CUDA）")
            ctx.set_stage_result("STEREO_FUSION", "skipped", "未启用稠密重建（或无 CUDA）")

        mesh_ply: Path | None = None
        if self.enable_mesh and fused_ply:
            try:
                mesh_ply = self._stage_meshing(fused_ply)
            except PipelineError as e:
                if e.code == "CANCELLED":
                    raise
                # 网格生成失败/过慢不阻断任务：降级为点云产物并警告
                result["warnings"].append(f"网格生成失败，已降级为点云导出: {e.message}")
                ctx.job_log(f"[MESHING] 失败已降级: {e.message}")
                ctx.set_stage_result("MESHING", "skipped", f"网格生成失败，已降级为点云导出（{e.message}）")
        else:
            ctx.set_stage_result("MESHING", "skipped", "未启用网格生成或无稠密点云输入")

        photo_glb = self._stage_texturing(dense_dir, mesh_ply, result)
        self._stage_export(model_dir, fused_ply, mesh_ply, result, photo_glb=photo_glb)
        return result

    # ---- 各阶段 ----

    def _stage_image_validation(self) -> None:
        ctx = self.ctx
        stage = "IMAGE_VALIDATION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        images_dir = ctx.work_dir / "images"
        entries = ctx.input_manifest.get("images", [])
        names = [
            f"img_{i:04d}_{sanitize_filename(Path(entry['filename']).stem)}{Path(entry['filename']).suffix.lower()}"
            for i, entry in enumerate(entries)
        ]
        for entry in entries:
            ctx.check_cancel(stage)
            src = Path(entry["stored_path"])
            if not src.is_file() or sha256_file(src) != entry["checksum"]:
                raise PipelineError(stage, "INPUT_CHANGED", f"输入文件丢失或已改变: {entry['filename']}")
        if (images_dir.is_dir() and set(p.name for p in images_dir.iterdir()) == set(names)
                and ctx.try_resume_stage(stage, stage_dir, ["validated.json", *(images_dir / n for n in names)])):
            return
        (stage_dir / "manifest.json").unlink(missing_ok=True)
        ctx.set_stage_running(stage)
        t0 = time.time()
        if len(entries) < 3:
            raise PipelineError(stage, "TOO_FEW_IMAGES", f"有效图片不足 3 张（{len(entries)}），无法重建")

        # COLMAP resolves symlinks, so build a clean flat directory of copies.
        _clear_dir(images_dir)
        images_dir.mkdir(parents=True)
        valid_entries = []
        with open(ctx.log_file(stage), "a", encoding="utf-8") as log:
            for entry, name in zip(entries, names):
                ctx.check_cancel(stage)
                copied = images_dir / name
                shutil.copy2(entry["stored_path"], copied)
                if sha256_file(copied) != entry["checksum"]:
                    raise PipelineError(stage, "INPUT_CHANGED", f"复制时输入文件已改变: {entry['filename']}")
                valid_entries.append({"name": name, "image_id": entry["image_id"], "checksum": entry["checksum"]})
                log.write(f"OK {name} <- {entry['filename']}\n")

        atomic_write_json(stage_dir / "validated.json", {"images": valid_entries, "problems": []})
        ctx.write_stage_manifest(stage, stage_dir, ["validated.json", *(images_dir / n for n in names)])
        msg = f"校验完成：{len(valid_entries)} 张有效"
        ctx.set_stage_result(stage, "completed", msg)
        ctx.job_log(f"[{stage}] {msg} 耗时 {time.time()-t0:.1f}s")

    def _stage_feature_extraction(self, work: Path) -> None:
        ctx = self.ctx
        stage = "FEATURE_EXTRACTION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        db_path = stage_dir / "database.db"
        upstream = ("IMAGE_VALIDATION",)
        if ctx.try_resume_stage(stage, stage_dir, [db_path], upstream):
            return
        ctx.set_stage_running(stage)
        _clear_database(db_path)
        self.adapter.feature_extraction(db_path, ctx.work_dir / "images", ctx.log_file(stage), self._cc,
                                        max_features=ctx.params["max_features"])
        _checkpoint_database(db_path)
        ctx.write_stage_manifest(stage, stage_dir, [db_path], upstream)
        ctx.set_stage_result(stage, "completed", "SIFT 特征提取完成")

    def _stage_feature_matching(self, work: Path) -> str:
        ctx = self.ctx
        stage = "FEATURE_MATCHING"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        matcher = ctx.config.get("matcher", "auto")
        if matcher == "auto":
            matcher = ctx.params["matcher_default"]
        upstream = ("FEATURE_EXTRACTION",)
        db_path = stage_dir / "database.db"
        if ctx.try_resume_stage(stage, stage_dir, [db_path], upstream):
            return matcher
        ctx.set_stage_running(stage)
        _clear_database(db_path)
        shutil.copy2(ctx.checkpoints_dir / "FEATURE_EXTRACTION" / "database.db", db_path)
        self.adapter.feature_matching(db_path, matcher, ctx.log_file(stage), self._cc)
        _checkpoint_database(db_path)
        ctx.write_stage_manifest(stage, stage_dir, [db_path], upstream)
        ctx.set_stage_result(stage, "completed", f"特征匹配完成（策略 {matcher}）")
        return matcher

    def _stage_sparse(self, work: Path) -> Path:
        ctx = self.ctx
        self._stage_feature_matching(work)
        stage = "SPARSE_RECONSTRUCTION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        upstream = ("FEATURE_MATCHING",)
        ckpt_model_dir = stage_dir / "model"
        required = [ckpt_model_dir / name for name in SPARSE_MODEL_FILES]
        if ctx.try_resume_stage(stage, stage_dir, required, upstream):
            return ckpt_model_dir
        ctx.set_stage_running(stage)
        _clear_dir(work / "sparse")
        _clear_dir(ckpt_model_dir)
        # Mapper receives a disposable DB, so even an engine that writes to it
        # cannot invalidate the matching checkpoint.
        scratch_db = work / "mapper.db"
        _clear_database(scratch_db)
        shutil.copy2(ctx.checkpoints_dir / "FEATURE_MATCHING" / "database.db", scratch_db)
        try:
            model_dir = self.adapter.sparse_mapping(
                scratch_db, ctx.work_dir / "images", work / "sparse", ctx.log_file(stage), self._cc
            )
        finally:
            _clear_database(scratch_db)
        if not all((model_dir / name).is_file() for name in SPARSE_MODEL_FILES):
            raise PipelineError(stage, "NO_SPARSE_MODEL", "稀疏模型文件不完整")
        ckpt_model_dir.mkdir()
        files = []
        for p in model_dir.glob("*.bin"):
            dest = ckpt_model_dir / p.name
            shutil.copy2(p, dest)
            files.append(dest)
        ctx.write_stage_manifest(stage, stage_dir, files, upstream)
        ctx.set_stage_result(stage, "completed", f"稀疏重建完成：{model_dir.name}")
        ctx.job_log(f"[{stage}] 稀疏模型目录: {ckpt_model_dir}")
        return ckpt_model_dir

    def _stage_undistort(self, work: Path, model_dir: Path, dense_dir: Path) -> None:
        ctx = self.ctx
        stage = "IMAGE_UNDISTORTION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        upstream = ("SPARSE_RECONSTRUCTION",)
        required = _undistortion_files(dense_dir)
        if required and ctx.try_resume_stage(stage, stage_dir, required, upstream):
            return
        (stage_dir / "manifest.json").unlink(missing_ok=True)
        ctx.set_stage_running(stage)
        _clear_dir(dense_dir)
        self.adapter.undistort(ctx.work_dir / "images", model_dir, dense_dir, ctx.log_file(stage), self._cc)
        required = _undistortion_files(dense_dir)
        if not required:
            raise PipelineError(stage, "NO_OUTPUT", "去畸变工作区不完整")
        ctx.write_stage_manifest(stage, stage_dir, required, upstream)
        ctx.set_stage_result(stage, "completed", "图像去畸变完成")

    def _stage_dense(self, dense_dir: Path) -> None:
        ctx = self.ctx
        stage = "DENSE_RECONSTRUCTION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        upstream = ("IMAGE_UNDISTORTION",)
        required = _stereo_files(dense_dir)
        if required and ctx.try_resume_stage(stage, stage_dir, required, upstream):
            return
        (stage_dir / "manifest.json").unlink(missing_ok=True)
        ctx.set_stage_running(stage)
        for name in ("depth_maps", "normal_maps", "consistency_graphs"):
            folder = dense_dir / "stereo" / name
            _clear_dir(folder)
            folder.mkdir(parents=True)
        self.adapter.dense_stereo(dense_dir, ctx.log_file(stage), self._cc,
                                  max_image_size=ctx.dense_max_image_size)
        files = _stereo_files(dense_dir)
        if not files:
            raise PipelineError(stage, "NO_OUTPUT", "COLMAP 未生成完整的几何深度图和法线图")
        ctx.write_stage_manifest(stage, stage_dir, files, upstream)
        ctx.set_stage_result(stage, "completed", "稠密深度图完成（GPU）")

    def _stage_fusion(self, dense_dir: Path) -> Path:
        ctx = self.ctx
        stage = "STEREO_FUSION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        upstream = ("DENSE_RECONSTRUCTION",)
        if ctx.try_resume_stage(stage, stage_dir, ["fused.ply"], upstream):
            return stage_dir / "fused.ply"
        ctx.set_stage_running(stage)
        out_ply = stage_dir / "fused.ply"
        out_ply.unlink(missing_ok=True)
        self.adapter.stereo_fusion(dense_dir, out_ply, ctx.log_file(stage), self._cc,
                                   max_image_size=ctx.dense_max_image_size)
        _require_ply(stage, out_ply)
        ctx.write_stage_manifest(stage, stage_dir, [out_ply], upstream)
        ctx.set_stage_result(stage, "completed", f"点云融合完成（{human_size(out_ply.stat().st_size)}）")
        return out_ply

    def _stage_meshing(self, fused_ply: Path) -> Path:
        ctx = self.ctx
        stage = "MESHING"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        upstream = ("STEREO_FUSION",)
        if ctx.try_resume_stage(stage, stage_dir, ["meshed-poisson.ply"], upstream):
            return stage_dir / "meshed-poisson.ply"
        ctx.set_stage_running(stage)
        out_ply = stage_dir / "meshed-poisson.ply"
        out_ply.unlink(missing_ok=True)
        # 离群点过滤：远距离离群点会使 Poisson 八叉树覆盖巨大空域，耗时呈数量级增长
        mesh_input = fused_ply
        filtered = stage_dir / "fused-filtered.ply"
        try:
            removed = _filter_cloud_outliers(fused_ply, filtered)
            if removed > 0 and filtered.is_file():
                mesh_input = filtered
                ctx.job_log(f"[{stage}] 离群点过滤：移除 {removed} 点")
        except Exception as e:
            ctx.job_log(f"[{stage}] 离群点过滤失败，使用原始点云: {e}")
        from app.reconstruction.adapter import ColmapStageError

        try:
            self.adapter.poisson_meshing(
                mesh_input, out_ply, ctx.log_file(stage), self._cc,
                quality=ctx.params["poisson_depth"], num_threads=POISSON_THREADS,
            )
        except ColmapStageError as exc:
            raise PipelineError(stage, exc.code, str(exc)) from exc
        _require_ply(stage, out_ply, mesh=True)
        ctx.write_stage_manifest(stage, stage_dir, [out_ply], upstream)
        ctx.set_stage_result(stage, "completed", f"Poisson 网格生成完成（{human_size(out_ply.stat().st_size)}）")
        return out_ply

    def _stage_texturing(self, dense_dir: Path, mesh_ply: Path | None, result: dict) -> Path | None:
        from tempfile import TemporaryDirectory

        from app.reconstruction.photo_export import PHOTO_FILENAME, PHOTO_NATIVE, PHOTO_STAGE, photo_mesh_to_glb
        from app.reconstruction.texturer import TEXTURING_OPTIONS, TEXTURE_SCALES, bake_texture, estimate_upright, resolve_texturer

        ctx = self.ctx
        stage = "TEXTURING"
        ctx.check_cancel(stage)
        stage_dir = ctx.checkpoints_dir / stage
        configured_bin = get_settings().colmap_texturer_bin
        if not configured_bin or mesh_ply is None:
            reason = "未配置 COLMAP_TEXTURER_BIN" if not configured_bin else "无可贴图网格"
            result["photo_texture"] = {"status": "skipped", "reason": reason}
            ctx.set_stage_result(stage, "skipped", reason)
            return None

        upstream = ("IMAGE_UNDISTORTION", "MESHING")
        stage_dir.mkdir(parents=True, exist_ok=True)
        glb = stage_dir / PHOTO_FILENAME
        metadata_path = stage_dir / "photo_texture.json"
        ctx.set_stage_running(stage)
        try:
            executable = resolve_texturer(configured_bin)
            binary = Path(executable).stat()
            implementation = {
                "version": 2, "scales": list(TEXTURE_SCALES), "binary": executable, "size": binary.st_size,
                "mtime_ns": binary.st_mtime_ns, "options": TEXTURING_OPTIONS,
            }
            try:
                saved = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
            if isinstance(saved, dict) and saved.get("implementation") == implementation:
                if ctx.try_resume_stage(stage, stage_dir, [glb.resolve(), metadata_path.resolve()], upstream):
                    result["photo_texture"] = saved
                    return glb
            (stage_dir / "manifest.json").unlink(missing_ok=True)
            with TemporaryDirectory(prefix="bake-", dir=stage_dir) as temp:
                scratch = Path(temp)
                log = ctx.log_file(stage)
                check_cancel = lambda: ctx.check_cancel(stage)
                textured = bake_texture(executable, dense_dir, mesh_ply, scratch / "texture", log, check_cancel)
                try:
                    up = estimate_upright(executable, dense_dir, scratch, log, result.get("display_up"), check_cancel)
                except Exception as exc:
                    if getattr(exc, "code", None) == "CANCELLED":
                        raise
                    ctx.job_log(f"[{stage}] 朝向估计不可用: {exc}")
                    up = None
                check_cancel()
                metadata = photo_mesh_to_glb(mesh_ply, textured, scratch / PHOTO_FILENAME, up)
                check_cancel()
                (scratch / PHOTO_FILENAME).replace(glb)
            metadata.update(status="completed", path=PHOTO_FILENAME, implementation=implementation,
                            origin=PHOTO_STAGE if up is not None else PHOTO_NATIVE)
            atomic_write_json(metadata_path, metadata)
            ctx.write_stage_manifest(stage, stage_dir, [glb.resolve(), metadata_path.resolve()], upstream)
        except Exception as exc:
            if getattr(exc, "code", None) == "CANCELLED":
                raise
            ctx.check_cancel(stage)
            message = f"照片贴图不可用，保留原有模型导出: {exc}"
            result["warnings"].append(message)
            result["photo_texture"] = {"status": "fallback", "reason": str(exc)}
            ctx.job_log(f"[{stage}] {message}")
            ctx.set_stage_result(stage, "skipped", message)
            return None
        result["photo_texture"] = metadata
        ctx.set_stage_result(stage, "completed", "照片贴图完成" if up is not None else "照片贴图完成，保留原朝向供手动调整")
        return glb

    def _stage_export(
        self, model_dir: Path, fused_ply: Path | None, mesh_ply: Path | None, result: dict,
        *, photo_glb: Path | None = None,
    ) -> None:
        ctx = self.ctx
        stage = "EXPORTING"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        upstream = tuple(name for name, present in (
            ("SPARSE_RECONSTRUCTION", True), ("STEREO_FUSION", fused_ply is not None),
            ("MESHING", mesh_ply is not None), ("TEXTURING", photo_glb is not None),
        ) if present)
        required = ["exported.json"]
        if photo_glb is not None:
            required.append(str((ctx.out_dir / photo_glb.name).resolve()))
        if ctx.try_resume_stage(stage, stage_dir, required, upstream):
            saved = json.loads((stage_dir / "exported.json").read_text(encoding="utf-8"))
            result["warnings"].extend(w for w in saved.get("warnings", []) if w not in result["warnings"])
            return
        ctx.check_cancel(stage)
        ctx.set_stage_running(stage)
        out = ctx.out_dir
        from app.reconstruction.photo_export import PHOTO_FILENAME

        # No stale photo file should survive a fallback or a changed checkpoint.
        (out / PHOTO_FILENAME).unlink(missing_ok=True)
        exported_path = stage_dir / "exported.json"
        exported_path.unlink(missing_ok=True)
        exported: list[dict] = []

        # 1) 稀疏点云（小，供点云展示）
        try:
            sparse_ply_out = out / "sparse_pointcloud.ply"
            _export_sparse_to_ply(model_dir, sparse_ply_out)
            if sparse_ply_out.is_file():
                exported.append({"type": "sparse_pointcloud", "format": "ply", "path": sparse_ply_out.name, "origin": stage})
        except Exception as e:
            result["warnings"].append(f"稀疏点云导出失败: {e}")

        # 2) 稠密点云
        if fused_ply:
            dense_out = out / "dense_pointcloud.ply"
            shutil.copy2(fused_ply, dense_out)
            exported.append({"type": "dense_pointcloud", "format": "ply", "path": dense_out.name, "origin": "STEREO_FUSION"})
            # 点云 GLB（Web 展示）
            glb_pc = out / "dense_pointcloud.glb"
            if export_tools.pointcloud_to_glb(dense_out, glb_pc):
                exported.append({"type": "dense_pointcloud", "format": "glb", "path": glb_pc.name, "origin": "STEREO_FUSION"})

        # 3) 网格（着色 + GLB）
        if mesh_ply:
            colored = out / "mesh_colored.ply"
            try:
                changed = (
                    fused_ply
                    and export_tools.colorize_mesh_from_cloud(mesh_ply, fused_ply, colored)
                )
                mesh_for_glb = colored if changed and colored.is_file() else mesh_ply
            except Exception:
                mesh_for_glb = mesh_ply
            if mesh_for_glb is not mesh_ply and not mesh_for_glb.is_file():
                mesh_for_glb = mesh_ply
            shutil.copy2(mesh_for_glb, out / "mesh.ply")
            exported.append({"type": "mesh", "format": "ply", "path": "mesh.ply", "origin": "MESHING"})
            glb = out / "model.glb"
            glb.unlink(missing_ok=True)
            try:
                export_tools.mesh_to_glb(out / "mesh.ply", glb)
                exported.append({"type": "web", "format": "glb", "path": glb.name, "origin": "EXPORTING"})
            except Exception as exc:
                glb.unlink(missing_ok=True)
                result["warnings"].append(f"基础 GLB 导出失败，保留 PLY: {exc}")

        if photo_glb is not None:
            from tempfile import NamedTemporaryFile

            tmp_path = None
            try:
                with NamedTemporaryFile(prefix="photo-", suffix=".tmp", dir=out, delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                shutil.copy2(photo_glb, tmp_path)
                ctx.check_cancel(stage)
                tmp_path.replace(out / PHOTO_FILENAME)
            except OSError as exc:
                message = f"照片 GLB 发布失败，保留原有模型导出: {exc}"
                result["warnings"].append(message)
                result["photo_texture"] = {"status": "fallback", "reason": str(exc)}
                ctx.job_log(f"[{stage}] {message}")
            else:
                exported.append({"type": "web", "format": "glb", "path": PHOTO_FILENAME,
                                 "origin": result["photo_texture"]["origin"]})
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)

        # 4) 预览图（优先稠密点云）
        pointcloud = next((item for item in reversed(exported) if item["type"] in
                           ("dense_pointcloud", "sparse_pointcloud") and item["format"] == "ply"), None)
        if pointcloud and export_tools.make_preview_png(out / pointcloud["path"], out / "preview.png"):
            exported.append({"type": "preview", "format": "png", "path": "preview.png", "origin": "EXPORTING"})

        if not exported or all(item["type"] == "preview" for item in exported):
            raise PipelineError(stage, "NO_OUTPUT", "没有任何可导出的模型产物")

        atomic_write_json(exported_path, {"exported": exported, "warnings": result["warnings"]})
        # A transient photo publication failure should be retried on resume.
        if photo_glb is None or result["photo_texture"]["status"] == "completed":
            ctx.write_stage_manifest(stage, stage_dir,
                                     [exported_path.resolve(), *((out / item["path"]).resolve() for item in exported)],
                                     upstream)
        ctx.set_stage_result(stage, "completed", f"导出 {len(exported)} 个产物")
        ctx.job_log(f"[{stage}] 导出清单: {[e['path'] for e in exported]}")


def _export_sparse_to_ply(model_dir: Path, out_ply: Path) -> None:
    """COLMAP 二进制稀疏模型 → 带颜色 PLY 点云。"""
    try:
        import pycolmap  # type: ignore
    except ImportError:
        pycolmap = None
    if pycolmap is not None:
        rec = pycolmap.Reconstruction(str(model_dir))
        rec.export_PLY(str(out_ply))
        return
    # 手工解析 points3D.bin（避免强依赖 pycolmap）
    _export_points3d_bin(model_dir / "points3D.bin", out_ply)


def _export_points3d_bin(bin_path: Path, out_ply: Path) -> None:
    if not bin_path.is_file():
        raise FileNotFoundError(bin_path)
    import struct

    data = bin_path.read_bytes()
    off = 0
    (num_points,) = struct.unpack_from("<Q", data, off)
    off += 8
    pts, cols = [], []
    for _ in range(num_points):
        (pid,) = struct.unpack_from("<Q", data, off)
        off += 8
        x, y, z = struct.unpack_from("<ddd", data, off)
        off += 24
        r, g, b = struct.unpack_from("<BBB", data, off)
        off += 3
        (err,) = struct.unpack_from("<d", data, off)
        off += 8
        (track_len,) = struct.unpack_from("<Q", data, off)
        off += 8
        off += track_len * 8  # 跳过 track（image_id + point2D_idx，各 int32）
        pts.append((x, y, z))
        cols.append((r, g, b))
    _write_colored_ply(out_ply, np.asarray(pts), np.asarray(cols, dtype=np.uint8))


def _write_colored_ply(out_ply: Path, pts: "np.ndarray", cols: "np.ndarray") -> None:
    header = (
        "ply\nformat ascii 1.0\n"
        f"element vertex {len(pts)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n"
    )
    with open(out_ply, "w") as f:
        f.write(header)
        for p, c in zip(pts, cols):
            f.write(f"{p[0]:.4f} {p[1]:.4f} {p[2]:.4f} {c[0]} {c[1]} {c[2]}\n")


import numpy as np  # noqa: E402  供 _write_colored_ply 使用


def _clear_dir(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _clear_database(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _checkpoint_database(path: Path) -> None:
    # The immutable DB must contain committed data, not rely on a WAL sidecar.
    from contextlib import closing

    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db:
        busy, *_ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy or db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise RuntimeError(f"Could not finalize COLMAP database: {path}")


def _fusion_names(dense_dir: Path) -> list[str]:
    config = dense_dir / "stereo" / "fusion.cfg"
    if not config.is_file():
        return []
    names = [line.strip() for line in config.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
        return []
    return names


def _undistortion_files(dense_dir: Path) -> list[Path]:
    sparse_dir = dense_dir / "sparse"
    if not sparse_dir.is_dir():
        sparse_dir = dense_dir
    names = _fusion_names(dense_dir)
    if not names:
        return []
    model = [sparse_dir / name for name in SPARSE_MODEL_FILES]
    configs = [dense_dir / "stereo" / name for name in ("patch-match.cfg", "fusion.cfg")]
    images = [dense_dir / "images" / name for name in names]
    files = [*model, *sparse_dir.glob("*.bin"), *configs, *images]
    return files if all(p.is_file() and p.stat().st_size for p in files) else []


def _stereo_files(dense_dir: Path) -> list[Path]:
    stereo = dense_dir / "stereo"
    names = _fusion_names(dense_dir)
    if not names:
        return []
    required = [stereo / subdir / f"{name}.geometric.bin"
                for subdir in ("depth_maps", "normal_maps") for name in names]
    if not all(p.is_file() and p.stat().st_size for p in required):
        return []
    optional = [p for folder in ("depth_maps", "normal_maps", "consistency_graphs")
                for p in (stereo / folder).rglob("*.bin") if p.is_file() and p.stat().st_size]
    return [*required, *optional]


def _require_ply(stage: str, path: Path, mesh: bool = False) -> None:
    stats = export_tools.ply_stats(path) if path.is_file() else {}
    if not stats.get("vertex_count") or (mesh and not stats.get("face_count")):
        raise PipelineError(stage, "NO_OUTPUT", "未生成有效 PLY 产物")


def _filter_cloud_outliers(ply_path: Path, out_path: Path, pct: float = 0.5) -> int:
    """按坐标百分位裁剪远距离离群点（针对 stereo_fusion 的二进制 PLY）。

    逐轴取 [pct, 100-pct] 百分位并外扩 5% 域作为保留盒；返回被移除的点数。
    解析失败时抛异常，由调用方回退为原始点云。
    """
    with open(ply_path, "rb") as f:
        header_lines = []
        while True:
            line = f.readline()
            if not line:
                raise ValueError("Incomplete PLY header")
            header_lines.append(line.decode("ascii", errors="replace").strip())
            if line.strip() == b"end_header":
                break
        text = "\n".join(header_lines)
        if "format binary_little_endian" not in text:
            raise ValueError("仅支持 binary_little_endian PLY")
        # 解析 vertex 属性
        in_vertex = False
        props: list[tuple[str, str]] = []
        n_points = 0
        for ln in header_lines:
            if ln.startswith("element"):
                in_vertex = ln.split()[1] == "vertex"
                if in_vertex:
                    n_points = int(ln.split()[2])
            elif ln.startswith("property") and in_vertex:
                parts = ln.split()
                props.append((parts[1], parts[2]))
        if not n_points or not props:
            raise ValueError("无顶点数据")

        dtype = np.dtype([(name, {"float": "<f4", "float32": "<f4", "uchar": "u1", "uint8": "u1"}.get(t)) for t, name in props])
        data = np.fromfile(f, dtype=dtype, count=n_points)

    x = np.stack([data["x"], data["y"], data["z"]], axis=1)
    lo = np.percentile(x, pct, axis=0)
    hi = np.percentile(x, 100 - pct, axis=0)
    margin = (hi - lo) * 0.05 + 1e-6
    keep = np.all((x >= lo - margin) & (x <= hi + margin), axis=1)
    removed = int((~keep).sum())
    if removed == 0 or keep.sum() < 1000:
        return 0  # 无明显离群点，或过滤后过少：不启用过滤

    filtered = data[keep]
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(filtered)}\n"
        + "".join(f"property {t} {n}\n" for t, n in props)
        + "end_header\n"
    )
    with open(out_path, "wb") as f:
        f.write(header.encode("ascii"))
        f.write(filtered.tobytes())
    return removed
