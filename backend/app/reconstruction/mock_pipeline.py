"""Mock 重建管线 — 无 COLMAP 环境下模拟全流程（本地开发 / 前端联调 / 演示）。

与真实管线共用 PipelineContext：阶段事件、进度、检查点、取消逻辑一致。
产物为程序生成的合成点云/网格/GLB，明确标注 pipeline_mode=mock。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from app.core.logging_setup import get_logger
from app.reconstruction.export import make_preview_png
from app.reconstruction.manifest import atomic_write_json
from app.reconstruction.pipeline import PipelineContext, StageCancelled

logger = get_logger("mock")


def _delay(stage: str) -> float:
    import os

    scale = float(os.environ.get("MOCK_DELAY_SCALE", "1.0"))
    base = {
        "IMAGE_VALIDATION": 0.4,
        "FEATURE_EXTRACTION": 0.8,
        "FEATURE_MATCHING": 0.8,
        "SPARSE_RECONSTRUCTION": 1.0,
        "IMAGE_UNDISTORTION": 0.3,
        "DENSE_RECONSTRUCTION": 1.5,
        "STEREO_FUSION": 0.8,
        "MESHING": 0.8,
        "EXPORTING": 0.5,
    }
    return base.get(stage, 0.5) * scale


def _synthetic_cloud(n: int, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    """带颜色合成点云（球体 + 底面圆盘）。"""
    rng = np.random.default_rng(seed)
    # 球面点
    n_sphere = int(n * 0.7)
    dirs = rng.normal(size=(n_sphere, 3))
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True) + 1e-12
    sphere = dirs * 1.0
    # 底面圆盘
    n_disk = n - n_sphere
    r = np.sqrt(rng.uniform(0, 1.2, n_disk))
    th = rng.uniform(0, 2 * np.pi, n_disk)
    disk = np.stack([r * np.cos(th), r * np.sin(th), -rng.uniform(0, 0.05, n_disk)], axis=1)
    pts = np.vstack([sphere, disk])
    hue = (np.linalg.norm(pts[:, :2], axis=1) / 1.2)
    cols = np.stack(
        [200 - hue * 120, 120 + hue * 80, 160 + hue * 60], axis=1
    ).clip(0, 255).astype(np.uint8)
    return pts, cols


def _write_ply(out: Path, pts: np.ndarray, cols: np.ndarray) -> None:
    import trimesh

    pc = trimesh.PointCloud(pts, colors=cols)
    out.parent.mkdir(parents=True, exist_ok=True)
    pc.export(out, file_type="ply")


class MockPipeline:
    def __init__(self, ctx: PipelineContext):
        self.ctx = ctx

    def run(self) -> dict:
        ctx = self.ctx
        result: dict = {"skipped_stages": [], "warnings": ["Mock 模式：产物为合成数据，非真实重建结果"]}
        stages = [
            "IMAGE_VALIDATION", "FEATURE_EXTRACTION", "FEATURE_MATCHING",
            "SPARSE_RECONSTRUCTION", "IMAGE_UNDISTORTION", "DENSE_RECONSTRUCTION",
            "STEREO_FUSION", "MESHING", "EXPORTING",
        ]
        enable_dense = bool(ctx.config.get("enable_dense", True))
        enable_mesh = bool(ctx.config.get("enable_mesh", True))
        skip_dense = not enable_dense
        skip_mesh = not enable_mesh or skip_dense

        ctx.engine_identity = {"mode": "mock", "version": "mock-v2"}
        completed: list[str] = []
        for stage in stages:
            stage_dir = ctx.checkpoints_dir / stage
            stage_dir.mkdir(parents=True, exist_ok=True)
            upstream = tuple(completed[-1:])
            if ctx.try_resume_stage(stage, stage_dir, [], upstream):
                completed.append(stage)
                continue
            ctx.check_cancel(stage)
            if (skip_dense and stage in ("IMAGE_UNDISTORTION", "DENSE_RECONSTRUCTION", "STEREO_FUSION")) or (
                skip_mesh and stage == "MESHING"
            ):
                ctx.set_stage_running(stage)
                time.sleep(_delay(stage) * 0.3)
                ctx.set_stage_result(stage, "skipped", "配置未启用（mock）")
                continue

            ctx.set_stage_running(stage)
            t0 = time.time()
            time.sleep(_delay(stage))
            if stage == "IMAGE_VALIDATION":
                n = ctx.input_manifest.get("image_count", 0)
                if n < 3:
                    from app.reconstruction.pipeline import PipelineError

                    raise PipelineError(stage, "TOO_FEW_IMAGES", f"有效图片不足 3 张（{n}）")
                (stage_dir / "validated.json").write_text(json.dumps({"count": n}), encoding="utf-8")
                ctx.write_stage_manifest(stage, stage_dir, ["validated.json"], upstream)
                ctx.set_stage_result(stage, "completed", f"校验完成：{n} 张（mock）")
            elif stage == "SPARSE_RECONSTRUCTION":
                pts, cols = _synthetic_cloud(6000, seed=42)
                _write_ply(stage_dir / "sparse.ply", pts, cols)
                ctx.write_stage_manifest(stage, stage_dir, ["sparse.ply"], upstream)
                ctx.set_stage_result(stage, "completed", "稀疏点云完成（mock）")
            elif stage == "DENSE_RECONSTRUCTION":
                atomic_write_json(stage_dir / "depth.done", {"stage": stage})
                ctx.write_stage_manifest(stage, stage_dir, ["depth.done"], upstream)
                ctx.set_stage_result(stage, "completed", "稠密深度图完成（mock）")
            elif stage == "STEREO_FUSION":
                pts, cols = _synthetic_cloud(150000, seed=7)
                _write_ply(stage_dir / "fused.ply", pts, cols)
                ctx.write_stage_manifest(stage, stage_dir, ["fused.ply"], upstream)
                ctx.set_stage_result(stage, "completed", f"点云融合完成（mock，{len(pts)} 点）")
            elif stage == "MESHING":
                import trimesh

                mesh = trimesh.creation.icosphere(subdivisions=4, radius=1.0)
                cols = np.tile(np.array([168, 190, 176], dtype=np.uint8), (len(mesh.vertices), 1))
                mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=cols)
                stage_dir.mkdir(parents=True, exist_ok=True)
                mesh.export(stage_dir / "meshed.ply", file_type="ply")
                ctx.write_stage_manifest(stage, stage_dir, ["meshed.ply"], upstream)
                ctx.set_stage_result(stage, "completed", "Poisson 网格完成（mock）")
            elif stage == "EXPORTING":
                payload = self._export(ctx)
                ctx.write_stage_manifest(stage, stage_dir,
                                         ["exported.json", *((ctx.out_dir / item["path"]).resolve()
                                                             for item in payload["exported"])], upstream)
                ctx.set_stage_result(stage, "completed", "导出完成（mock）")
            else:
                marker = "validated.json" if stage == "IMAGE_VALIDATION" else "done"
                atomic_write_json(stage_dir / marker, {"stage": stage})
                ctx.write_stage_manifest(stage, stage_dir, [marker], upstream)
                ctx.set_stage_result(stage, "completed", f"{stage} 完成（mock）")
            completed.append(stage)
            ctx.job_log(f"[{stage}] mock 完成，耗时 {time.time()-t0:.1f}s")
        return result

    def _export(self, ctx: PipelineContext) -> None:
        import trimesh

        out = ctx.out_dir
        exported: list[dict] = []
        sparse_ckpt = ctx.checkpoints_dir / "SPARSE_RECONSTRUCTION" / "sparse.ply"
        fused_ckpt = ctx.checkpoints_dir / "STEREO_FUSION" / "fused.ply"
        mesh_ckpt = ctx.checkpoints_dir / "MESHING" / "meshed.ply"

        if sparse_ckpt.is_file():
            import shutil

            shutil.copy2(sparse_ckpt, out / "sparse_pointcloud.ply")
            exported.append({"type": "sparse_pointcloud", "format": "ply", "path": "sparse_pointcloud.ply", "origin": "SPARSE_RECONSTRUCTION"})
        if fused_ckpt.is_file():
            import shutil

            shutil.copy2(fused_ckpt, out / "dense_pointcloud.ply")
            exported.append({"type": "dense_pointcloud", "format": "ply", "path": "dense_pointcloud.ply", "origin": "STEREO_FUSION"})
        if mesh_ckpt.is_file():
            import shutil

            shutil.copy2(mesh_ckpt, out / "mesh.ply")
            exported.append({"type": "mesh", "format": "ply", "path": "mesh.ply", "origin": "MESHING"})
            glb = out / "model.glb"
            mesh = trimesh.load(mesh_ckpt, force="mesh", process=True)
            glb.parent.mkdir(parents=True, exist_ok=True)
            mesh.export(glb, file_type="glb")
            exported.append({"type": "web", "format": "glb", "path": "model.glb", "origin": "EXPORTING"})
        elif fused_ckpt.is_file():
            from app.reconstruction.export import pointcloud_to_glb

            if pointcloud_to_glb(out / "dense_pointcloud.ply", out / "model.glb"):
                exported.append({"type": "web", "format": "glb", "path": "model.glb", "origin": "STEREO_FUSION"})

        preview_src = out / "dense_pointcloud.ply" if (out / "dense_pointcloud.ply").is_file() else out / "sparse_pointcloud.ply"
        if preview_src.is_file() and make_preview_png(preview_src, out / "preview.png"):
            exported.append({"type": "preview", "format": "png", "path": "preview.png", "origin": "EXPORTING"})

        payload = {"exported": exported, "warnings": []}
        atomic_write_json(ctx.checkpoints_dir / "EXPORTING" / "exported.json", payload)
        return payload
