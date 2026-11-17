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
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.logging_setup import get_logger
from app.models.entities import (
    PIPELINE_STAGES,
    JobStageEvent,
    Model,
    ModelVersion,
    ReconstructionJob,
)
from app.reconstruction import export as export_tools
from app.reconstruction.manifest import (
    compute_sha_of,
    stage_checkpoint_valid,
    write_manifest,
)
from app.utils.files import human_size, sanitize_filename

logger = get_logger("pipeline")

PRESET_PARAMS = {
    "fast": {"max_features": 4096, "poisson_depth": 8, "matcher_default": "sequential"},
    "standard": {"max_features": 8192, "poisson_depth": 8, "matcher_default": "exhaustive"},
    "high": {"max_features": 16384, "poisson_depth": 10, "matcher_default": "exhaustive"},
}


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

        self.work_dir = project_job_dir(project, job.id)
        self.out_dir = project_output_dir(project, job.id)
        self.logs_dir = self.work_dir / "logs"
        self.checkpoints_dir = self.work_dir / "checkpoints"
        for d in (self.work_dir, self.out_dir, self.logs_dir, self.checkpoints_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.params = PRESET_PARAMS.get(config.get("preset", "standard"), PRESET_PARAMS["standard"])
        self.current_stage: str | None = None  # 供异常处理读取（不依赖 DB）
        # 恢复判定输入：任务级（输入清单 + 配置）
        self.job_inputs_sha = compute_sha_of(
            {"input_manifest": input_manifest, "config": config}
        )

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
            job = db.get(ReconstructionJob, self.job_id)
            job.status = "RUNNING"
            job.current_stage = stage
            db.add(JobStageEvent(job_id=job.id, stage=stage, status="running", started_at=_now()))
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
                done = self._completed_stages(db) | {stage}
                job.progress = float(sum(weights[s] for s in done if s in weights))
            db.commit()

    def _completed_stages(self, db: Session) -> set[str]:
        rows = db.execute(
            select(JobStageEvent.stage).where(
                JobStageEvent.job_id == self.job_id,
                JobStageEvent.status.in_(["completed", "skipped"]),
            )
        ).all()
        return {r[0] for r in rows}

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
            **({"poisson_depth": self.params["poisson_depth"]} if stage == "MESHING" else {}),
            **({"max_features": self.params["max_features"]} if stage == "FEATURE_EXTRACTION" else {}),
        }

    def try_resume_stage(self, stage: str, stage_dir: Path, required_hint: list[str]) -> bool:
        """检查点校验；有效则记录 skipped 事件并返回 True。"""
        params = self.stage_params(stage)
        inputs = {"job_inputs_sha": self.job_inputs_sha}
        if stage_checkpoint_valid(stage_dir, stage, params, inputs):
            self.set_stage_result(stage, "skipped", message="检查点有效，跳过（断点恢复）")
            self.job_log(f"[{stage}] 检查点有效，跳过")
            return True
        # 兼容：required_hint 仅用于日志提示
        return False


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

        self._stage_export(model_dir, fused_ply, mesh_ply, result)
        return result

    # ---- 各阶段 ----

    def _stage_image_validation(self) -> None:
        ctx = self.ctx
        stage = "IMAGE_VALIDATION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        if ctx.try_resume_stage(stage, stage_dir, ["validated.json"]):
            return
        ctx.check_cancel(stage)
        t0 = time.time()
        ctx.set_stage_running(stage)
        ctx.job_log(f"[{stage}] 校验 {ctx.input_manifest.get('image_count', 0)} 张输入图片")
        log = ctx.log_file(stage)

        # 创建 COLMAP 用扁平图片目录（符号链接，保留可读文件名）
        images_dir = ctx.work_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        problems: list[str] = []
        used_names: set[str] = set()
        valid_entries = []
        with open(log, "a", encoding="utf-8") as lf:
            for i, entry in enumerate(ctx.input_manifest.get("images", [])):
                src = Path(entry["stored_path"])
                if not src.is_file():
                    problems.append(f"{entry['filename']}: 文件丢失")
                    continue
                safe = sanitize_filename(Path(entry["filename"]).stem)
                name = f"img_{i:04d}_{safe}{Path(entry['filename']).suffix.lower()}"
                while name in used_names:
                    name = f"img_{i:04d}_{safe}_dup{i}{Path(entry['filename']).suffix.lower()}"
                used_names.add(name)
                link = images_dir / name
                if not link.exists():
                    # 注意：必须复制而非符号链接。COLMAP 3.10+ 会把符号链接解析为
                    # 规范路径并用作图片名，导致后续阶段（undistorter 等）沿绝对
                    # 路径创建目录而崩溃。
                    shutil.copy2(src, link)
                valid_entries.append({"name": name, "image_id": entry["image_id"], "checksum": entry["checksum"]})
                lf.write(f"OK {name} <- {entry['filename']}\n")

        if len(valid_entries) < 3:
            raise PipelineError(stage, "TOO_FEW_IMAGES", f"有效图片不足 3 张（{len(valid_entries)}），无法重建")
        (stage_dir / "validated.json").write_text(
            json.dumps({"images": valid_entries, "problems": problems}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        write_manifest(stage_dir, stage, ctx.stage_params(stage), {"job_inputs_sha": ctx.job_inputs_sha}, ["validated.json"])
        msg = f"校验完成：{len(valid_entries)} 张有效" + (f"；{len(problems)} 张异常" if problems else "")
        ctx.set_stage_result(stage, "completed", msg)
        ctx.job_log(f"[{stage}] {msg} 耗时 {time.time()-t0:.1f}s")

    def _stage_feature_extraction(self, work: Path) -> None:
        ctx = self.ctx
        stage = "FEATURE_EXTRACTION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        if ctx.try_resume_stage(stage, stage_dir, ["database.db"]):
            return
        ctx.check_cancel(stage)
        ctx.set_stage_running(stage)
        db_path = work / "database.db"
        self.adapter.feature_extraction(db_path, ctx.work_dir / "images", ctx.log_file(stage), self._cc)
        write_manifest(stage_dir, stage, ctx.stage_params(stage), {"job_inputs_sha": ctx.job_inputs_sha}, [str(db_path)])
        ctx.set_stage_result(stage, "completed", "SIFT 特征提取完成")

    def _stage_feature_matching(self, work: Path) -> str:
        ctx = self.ctx
        stage = "FEATURE_MATCHING"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        if ctx.try_resume_stage(stage, stage_dir, ["database.db"]):
            return ctx.config.get("matcher", "auto")
        ctx.check_cancel(stage)
        ctx.set_stage_running(stage)
        matcher = ctx.config.get("matcher", "auto")
        if matcher == "auto":
            matcher = self.ctx.params["matcher_default"]
        db_path = work / "database.db"
        self.adapter.feature_matching(db_path, matcher, ctx.log_file(stage), self._cc)
        write_manifest(stage_dir, stage, ctx.stage_params(stage), {"job_inputs_sha": ctx.job_inputs_sha}, [str(db_path)])
        ctx.set_stage_result(stage, "completed", f"特征匹配完成（策略 {matcher}）")
        return matcher

    def _stage_sparse(self, work: Path) -> Path:
        ctx = self.ctx
        self._stage_feature_matching(work)
        stage = "SPARSE_RECONSTRUCTION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        if ctx.try_resume_stage(stage, stage_dir, ["sparse/0/cameras.bin"]):
            return self._find_sparse_model(work)
        ctx.check_cancel(stage)
        ctx.set_stage_running(stage)
        model_dir = self.adapter.sparse_mapping(
            work / "database.db", ctx.work_dir / "images", work / "sparse", ctx.log_file(stage), self._cc
        )
        # 将稀疏模型文件复制到检查点目录（体积小），使 manifest 校验自包含
        ckpt_model_dir = stage_dir / "model"
        ckpt_model_dir.mkdir(parents=True, exist_ok=True)
        rel_files = []
        for p in model_dir.glob("*.bin"):
            shutil.copy2(p, ckpt_model_dir / p.name)
            rel_files.append(f"model/{p.name}")
        write_manifest(stage_dir, stage, ctx.stage_params(stage), {"job_inputs_sha": ctx.job_inputs_sha}, rel_files)
        ctx.set_stage_result(stage, "completed", f"稀疏重建完成：{model_dir.name}")
        ctx.job_log(f"[{stage}] 稀疏模型目录: {model_dir}")
        return model_dir

    def _find_sparse_model(self, work: Path) -> Path:
        sparse_root = work / "sparse"
        if (sparse_root / "0" / "cameras.bin").is_file():
            return sparse_root / "0"
        candidates = sorted(p for p in sparse_root.iterdir() if (p / "cameras.bin").is_file()) if sparse_root.is_dir() else []
        if not candidates:
            raise PipelineError("SPARSE_RECONSTRUCTION", "NO_SPARSE_MODEL", "未找到稀疏模型输出")
        return candidates[0]

    def _stage_undistort(self, work: Path, model_dir: Path, dense_dir: Path) -> None:
        ctx = self.ctx
        stage = "IMAGE_UNDISTORTION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        if ctx.try_resume_stage(stage, stage_dir, ["dense/cameras.bin"]):
            return
        ctx.check_cancel(stage)
        ctx.set_stage_running(stage)
        self.adapter.undistort(ctx.work_dir / "images", model_dir, dense_dir, ctx.log_file(stage), self._cc)
        # COLMAP 3.10+ 输出布局为 dense/sparse/*.bin；3.7 为扁平 dense/*.bin
        marker = dense_dir / "sparse" / "cameras.bin"
        if not marker.is_file():
            marker = dense_dir / "cameras.bin"
        if not marker.is_file():
            raise PipelineError(stage, "NO_OUTPUT", "去畸变未生成相机模型")
        write_manifest(stage_dir, stage, ctx.stage_params(stage), {"job_inputs_sha": ctx.job_inputs_sha}, [str(marker)])
        ctx.set_stage_result(stage, "completed", "图像去畸变完成")

    def _stage_dense(self, dense_dir: Path) -> None:
        ctx = self.ctx
        stage = "DENSE_RECONSTRUCTION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        if ctx.try_resume_stage(stage, stage_dir, ["stereo/depth_maps.bin"]):
            return
        ctx.check_cancel(stage)
        ctx.set_stage_running(stage)
        self.adapter.dense_stereo(dense_dir, ctx.log_file(stage), self._cc)
        files = [str(dense_dir / "stereo" / "depth_maps.bin"), str(dense_dir / "stereo" / "normal_maps.bin")]
        write_manifest(stage_dir, stage, ctx.stage_params(stage), {"job_inputs_sha": ctx.job_inputs_sha}, files)
        ctx.set_stage_result(stage, "completed", "稠密深度图完成（GPU）")

    def _stage_fusion(self, dense_dir: Path) -> Path:
        ctx = self.ctx
        stage = "STEREO_FUSION"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        if ctx.try_resume_stage(stage, stage_dir, ["fused.ply"]):
            return stage_dir / "fused.ply"
        ctx.check_cancel(stage)
        ctx.set_stage_running(stage)
        out_ply = stage_dir / "fused.ply"
        self.adapter.stereo_fusion(dense_dir, out_ply, ctx.log_file(stage), self._cc)
        write_manifest(stage_dir, stage, ctx.stage_params(stage), {"job_inputs_sha": ctx.job_inputs_sha}, ["fused.ply"])
        ctx.set_stage_result(stage, "completed", f"点云融合完成（{human_size(out_ply.stat().st_size)}）")
        return out_ply

    def _stage_meshing(self, fused_ply: Path) -> Path:
        ctx = self.ctx
        stage = "MESHING"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        if ctx.try_resume_stage(stage, stage_dir, ["meshed-poisson.ply"]):
            return stage_dir / "meshed-poisson.ply"
        ctx.check_cancel(stage)
        ctx.set_stage_running(stage)
        out_ply = stage_dir / "meshed-poisson.ply"
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
        self.adapter.poisson_meshing(
            mesh_input, out_ply, ctx.log_file(stage), self._cc, quality=self.ctx.params["poisson_depth"]
        )
        write_manifest(
            stage_dir, stage, ctx.stage_params(stage), {"job_inputs_sha": ctx.job_inputs_sha},
            ["meshed-poisson.ply"] + ([str(filtered)] if mesh_input is filtered else []),
        )
        ctx.set_stage_result(stage, "completed", f"Poisson 网格生成完成（{human_size(out_ply.stat().st_size)}）")
        return out_ply

    def _stage_export(self, model_dir: Path, fused_ply: Path | None, mesh_ply: Path | None, result: dict) -> None:
        ctx = self.ctx
        stage = "EXPORTING"
        stage_dir = ctx.checkpoints_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        if ctx.try_resume_stage(stage, stage_dir, ["exported.json"]):
            return
        ctx.check_cancel(stage)
        ctx.set_stage_running(stage)
        out = ctx.out_dir
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
            export_tools.mesh_to_glb(out / "mesh.ply", glb)
            exported.append({"type": "web", "format": "glb", "path": glb.name, "origin": "EXPORTING"})

        # 4) 预览图（优先稠密点云）
        preview_src = (out / "dense_pointcloud.ply") if (out / "dense_pointcloud.ply").is_file() else (out / "sparse_pointcloud.ply")
        if preview_src.is_file():
            if export_tools.make_preview_png(preview_src, out / "preview.png"):
                exported.append({"type": "preview", "format": "png", "path": "preview.png", "origin": "EXPORTING"})

        if not exported:
            raise PipelineError(stage, "NO_OUTPUT", "没有任何可导出的模型产物")

        (stage_dir / "exported.json").write_text(
            json.dumps({"exported": exported, "warnings": result["warnings"]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        write_manifest(stage_dir, stage, ctx.stage_params(stage), {"job_inputs_sha": ctx.job_inputs_sha}, ["exported.json"])
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


def _filter_cloud_outliers(ply_path: Path, out_path: Path, pct: float = 0.5) -> int:
    """按坐标百分位裁剪远距离离群点（针对 stereo_fusion 的二进制 PLY）。

    逐轴取 [pct, 100-pct] 百分位并外扩 5% 域作为保留盒；返回被移除的点数。
    解析失败时抛异常，由调用方回退为原始点云。
    """
    import struct as _struct

    with open(ply_path, "rb") as f:
        header_lines = []
        while True:
            line = f.readline()
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
