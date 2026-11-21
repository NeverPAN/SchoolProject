"""通用局部更新（阶段 D1/D2）：任意新增照片注册 + 父坐标系局部稠密融合。

与受试种子模式（local_reconstruction）的区别：
- 不依赖固定种子文件、文件名或"恰好 3 张"照片；新增照片数量 1–32 张；
- 新增照片由 image_registrator 注册到父稀疏模型，逐张报告成功/失败；
- 父位姿必须逐字保持；父点云记录前缀逐字节不变；
- 输出仍然是独立"局部点云候选"（不替换父模型、不生成网格）。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from app.reconstruction.adapter import ColmapAdapter
from app.reconstruction.local_reconstruction import (GUARD, MAX_DISTANCE, MAX_IMAGE_SIZE, MIN_DISTANCE,
                                                     MIN_NEW_VOTES, PATCHMATCH_ITERATIONS, RIG_FILES,
                                                     SPARSE_FILES, STAGE_ORIGIN, _adapter, _box,
                                                     _checked_bytes, _check_file, _file, _inside,
                                                     _local_points, _poses, _prepare_dense, _read_ply,
                                                     _select_patch, _stage, _verify_lineage,
                                                     _write_ply, _xyz)
from app.reconstruction.manifest import input_manifest_sha
from app.reconstruction.pipeline import PipelineContext

MODE = "local_general"
MAX_NEW_IMAGES = 32
MAX_PARENT_DENSE_VIEWS = 16
NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}")


def new_photo_name(image_id: int, filename: str) -> str:
    """提交与执行共用的新增照片派生名（确定性，不依赖运行环境）。"""
    source = Path(filename)
    name = f"new_{image_id:06d}_{re.sub(r'[^A-Za-z0-9_.-]', '_', source.stem)}{source.suffix.lower()}"
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"Invalid derived photo name for upload {image_id}")
    return name


@contextmanager
def scratch_dir(prefix: str, parent: Path):
    """NFS 上 memmap 会留下 .nfs 哨兵文件；忽略清理失败，且不让清理掩盖真实异常。"""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _frozen_local_spec(config: dict):
    """校验提交时冻结的通用局部配置骨架。"""
    target_id, region_id = config.get("parent_model_id"), config.get("region_id")
    if type(target_id) is not int or target_id < 1 or type(region_id) is not int or region_id < 1:
        raise ValueError("Invalid frozen parent/region IDs")
    lo, hi = _box(config["bounds"]["min"], config["bounds"]["max"])
    parent = _file(config["parent_model"], ())
    cloud = Path(config["parent_cloud"]["path"])
    if not cloud.is_absolute() or not cloud.is_file():
        raise ValueError(f"Parent point cloud is missing: {cloud.name}")
    sparse_specs = config["parent_sparse"]
    if set(sparse_specs) not in (SPARSE_FILES, SPARSE_FILES | RIG_FILES):
        raise ValueError("Expected a COLMAP binary sparse model (optional paired rigs/frames)")
    sparse = {name: _file(spec, ()) for name, spec in sparse_specs.items()}
    database = _file(config["parent_database"], ())
    parents = {}
    for entry in config["parent_images"]:
        name = entry["name"]
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid frozen parent image name: {name}")
        parents[name] = _file({"path": entry["path"], "sha256": entry["sha256"]}, ())
    return target_id, region_id, lo, hi, parent, cloud, sparse, database, parents


def _new_image_specs(ctx: PipelineContext):
    """任务冻结的新增照片清单：input_manifest 即新增照片（与整链任务同结构）。"""
    entries = ctx.input_manifest["images"]
    if not 1 <= len(entries) <= MAX_NEW_IMAGES:
        raise ValueError(f"Expected 1–{MAX_NEW_IMAGES} frozen new photos")
    names = {}
    for entry in entries:
        if type(entry["image_id"]) is not int or entry["image_id"] < 1:
            raise ValueError("Expected frozen uploaded image IDs")
        name = new_photo_name(entry["image_id"], entry["filename"])
        if name in names:
            raise ValueError(f"Duplicate new photo name: {name}")
        names[name] = (_file({"path": entry["stored_path"], "sha256": entry["checksum"]}, ()), entry["image_id"])
    return names


def _register_new_views(adapter: ColmapAdapter, root: Path, parent_sparse: Path, images: Path,
                        parent_names: set[str], new_names: set[str], max_features: int,
                        log_file: Path, cancel, database: Path | None = None) -> tuple[Path, dict]:
    """D1：向父特征数据库追加新图特征 + 显式配对匹配 + image_registrator。

    父图的关键点索引在父数据库中保持原样，父模型轨迹才能与 image_registrator
    的对应图一致（全新数据库会让父轨迹索引错位并触发 COLMAP CHECK 中止）。
    """
    if database is None:
        database = root / "registration.db"
    new_list = root / "new-images.txt"
    new_list.write_text("\n".join(sorted(new_names)) + "\n", encoding="ascii")
    adapter.feature_extraction(database, images, log_file, cancel, max_features=max_features,
                               image_list=new_list)
    # 与 e7 实验一致：全库穷举匹配（父-父已有匹配会被 COLMAP 跳过），
    # 保证对应图覆盖全部图像，image_registrator 才能安全重建对应关系。
    cancel()
    adapter.feature_matching(database, "exhaustive", log_file, cancel)

    registered = root / "registered"
    adapter.image_registrator(database, parent_sparse, registered, log_file, cancel)
    poses = _poses(registered / "images.bin")
    parent_poses = _poses(parent_sparse / "images.bin")
    for name, (rot, trans, camera_id) in parent_poses.items():
        got = poses.get(name)
        if got is None or not np.array_equal(rot, got[0]) or not np.array_equal(trans, got[1]):
            raise ValueError(f"Parent camera pose changed during registration: {name}")
    report = {name: ("registered" if name in poses else "not_registered") for name in sorted(new_names)}
    registered_new = sorted(name for name, state in report.items() if state == "registered")
    if not registered_new:
        raise ValueError("没有新增照片能注册到父稀疏模型；请检查照片是否与父场景有足够重叠")
    return registered, {"registered": registered_new,
                        "failed": sorted(name for name, state in report.items() if state != "registered"),
                        "parent_poses_unchanged": True, "matcher": "exhaustive over parent+new database"}


def _select_dense_views(poses: dict, registered_new: list[str], lo: np.ndarray, hi: np.ndarray) -> list[str]:
    """D2：全部注册新视角 + 距选区最近的至多 MAX_PARENT_DENSE_VIEWS 张父视角。

    相机可能在数米外（半径过滤会把父视角清零，融合点会错过选区），
    所以按"最近 K 张"保底，不再设硬半径门槛。
    """
    center = (lo + hi) / 2
    parents = []
    for name, (rot, trans, _camera) in poses.items():
        if name in registered_new:
            continue
        parents.append((float(np.linalg.norm(-rot.T @ trans - center)), name))
    parents.sort()
    return sorted(registered_new) + [name for _, name in parents[:MAX_PARENT_DENSE_VIEWS]]


def run(ctx: PipelineContext, config: dict) -> tuple[list[dict], dict]:
    """任意新增照片的局部点云候选：注册 → 局部稠密 → 支持点筛选 → 独立发布。"""
    with scratch_dir("local-general-", ctx.work_dir) as directory:
        root = Path(directory)
        images, dense = root / "images", root / "dense"
        copied_cloud = root / "parent.ply"
        new_names: set[str] = set()
        with _stage(ctx, "IMAGE_VALIDATION") as cancel:
            (target_id, region_id, lo, hi, parent, cloud_path, sparse_files,
             database_spec, parent_assets) = _frozen_local_spec(config)
            _verify_lineage(ctx, target_id, region_id, parent[0], lo, hi)
            _check_file(parent, cancel)
            cloud_sha = _checked_bytes(cloud_path, cancel, copied_cloud)
            parent_points = _read_ply(copied_cloud)
            images.mkdir()
            sparse = root / "sparse"
            sparse.mkdir()
            for name, asset in sparse_files.items():
                _check_file(asset, cancel, sparse / name)
            registration_db = root / "registration.db"
            _check_file(database_spec, cancel, registration_db)
            photos = dict(parent_assets)
            new_ids = {}
            for name, (asset, image_id) in _new_image_specs(ctx).items():
                if name in photos:
                    raise ValueError(f"新增照片文件名与父模型照片冲突: {name}")
                photos[name] = asset
                new_ids[name] = image_id
                new_names.add(name)
            # 稀疏模型中的名称必须与冻结的父照片一一对应，才能复用注册匹配
            model_names = set(_poses(sparse / "images.bin"))
            if model_names != set(parent_assets):
                raise ValueError("Frozen parent image names do not match the parent sparse model")
            for name, asset in photos.items():
                _check_file(asset, cancel, images / name)
            adapter = _adapter(ctx)
            ctx.engine_identity = adapter.engine_identity
            ctx.job_log(f"[IMAGE_VALIDATION] 已验证父版本 #{target_id}、{len(parent_assets)} 张父照片、"
                        f"{len(new_names)} 张新增照片")

        resolved = config["resolved"]
        with _stage(ctx, "REGISTER_NEW_VIEWS"):
            registered, registration = _register_new_views(
                adapter, root, sparse, images, set(parent_assets), new_names,
                max_features=resolved["max_features"], log_file=ctx.log_file("REGISTER_NEW_VIEWS"),
                cancel=lambda: ctx.check_cancel("REGISTER_NEW_VIEWS"), database=registration_db)
            registered_new = registration["registered"]
            ctx.job_log(f"[REGISTER_NEW_VIEWS] 注册 {len(registered_new)}/{len(new_names)} 张新增照片"
                        + (f"；未注册：{'、'.join(registration['failed'])}" if registration["failed"] else ""))

        with _stage(ctx, "IMAGE_UNDISTORTION"):
            poses = _poses(registered / "images.bin")
            view_names = _select_dense_views(poses, registered_new, lo, hi)
            ctx.job_log(f"[IMAGE_UNDISTORTION] 局部稠密视角 {len(view_names)} 个"
                        f"（{len(registered_new)} 新 + {len(view_names) - len(registered_new)} 父）")
            _prepare_dense(adapter, images, registered, dense, view_names,
                           ctx.log_file("IMAGE_UNDISTORTION"), ctx.cancel_check)

        with _stage(ctx, "DENSE_RECONSTRUCTION"):
            adapter.dense_stereo(dense, ctx.log_file("DENSE_RECONSTRUCTION"), ctx.cancel_check,
                                 max_image_size=MAX_IMAGE_SIZE,
                                 num_iterations=resolved.get("patchmatch_iterations", PATCHMATCH_ITERATIONS))

        votes_needed = min(MIN_NEW_VOTES, len(registered_new))
        with _stage(ctx, "STEREO_FUSION") as cancel:
            fused = dense / "fused.ply"
            adapter.stereo_fusion(dense, fused, ctx.log_file("STEREO_FUSION"), ctx.cancel_check,
                                  max_image_size=MAX_IMAGE_SIZE, num_threads=ctx.fusion_threads)
            patch, counts = _select_patch(parent_points, _read_ply(fused), dense, registered_new,
                                          lo + GUARD, hi - GUARD, cancel, min_votes=votes_needed)
            ctx.job_log(f"[STEREO_FUSION] 核心区新增 {counts['added_points']} 个支持点"
                        f"（{votes_needed} 视角一致性门槛）")

        with _stage(ctx, "EXPORTING") as cancel:
            out = ctx.out_dir / "local-fused.ply"
            total = len(parent_points) + len(patch)
            staging = Path(tempfile.mkdtemp(prefix=".local-export-", dir=ctx.out_dir))
            try:
                temp = staging / out.name
                _write_ply(temp, parent_points, patch, cancel)
                sha = _checked_bytes(temp, cancel)
                staged = _read_ply(temp)
                if len(staged) != total or not _inside(_xyz(patch), lo + GUARD, hi - GUARD).all():
                    raise ValueError("Local point-cloud output does not satisfy the guarded bounds")
                for start in range(0, len(parent_points), 131072):
                    cancel()
                    sl = slice(start, min(start + 131072, len(parent_points)))
                    if not np.array_equal(staged[sl].view("u1"), parent_points[sl].view("u1")):
                        raise ValueError("Parent point records changed in the child point cloud")
                _verify_lineage(ctx, config["parent_model_id"], config["region_id"], parent[0], lo, hi)
                _check_file(parent, cancel)
                _checked_bytes(cloud_path, cancel, expected=cloud_sha)
                size = temp.stat().st_size
                del staged
                cancel()
                os.replace(temp, out)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        del parent_points

    records = [{"model_type": "dense_pointcloud", "format": "ply", "file_path": str(out),
                "file_size": size, "stage_origin": STAGE_ORIGIN, "vertex_count": total,
                "face_count": 0, "point_count": total}]
    manifest = {
        "job_id": ctx.job_id, "pipeline_mode": MODE, "engine_version": adapter.version,
        "engine": ctx.engine_identity,
        "target_model_id": config["parent_model_id"], "parent_model_id": config["parent_model_id"],
        "region_id": config["region_id"], "geometry": "pointcloud_only",
        "note": "通用局部更新：任意新增照片注册到父稀疏模型后做局部稠密融合；只补充核心区点云，"
                "不替换父模型或生成网格。",
        "inputs_sha": input_manifest_sha(ctx.input_manifest),
        "parent_model_sha256": parent[1], "parent_cloud_sha256": cloud_sha,
        "sparse_sha256": {name: asset[1] for name, asset in sparse_files.items()},
        "parent_database_sha256": database_spec[1],
        "view_sha256": {name: asset[1] for name, asset in photos.items()},
        "registration": registration,
        "dense_views": view_names,
        "new_views": {name: new_ids[name] for name in registered_new},
        "resolved_params": {"bbox_min": lo.tolist(), "bbox_max": hi.tolist(),
                            "core_min": (lo + GUARD).tolist(), "core_max": (hi - GUARD).tolist(),
                            "guard_band": GUARD, "min_parent_distance": MIN_DISTANCE,
                            "max_parent_distance": MAX_DISTANCE,
                            "min_new_view_votes": votes_needed,
                            "relative_depth_tolerance": 0.012,
                            "dense_max_image_size": MAX_IMAGE_SIZE,
                            "patchmatch_iterations": resolved.get("patchmatch_iterations", PATCHMATCH_ITERATIONS),
                            "geom_consistency": True, "gpu_index": adapter.gpu_index,
                            "fusion_threads": ctx.fusion_threads,
                            "max_parent_dense_views": MAX_PARENT_DENSE_VIEWS,
                            "parent_view_policy": "nearest-K, no hard radius"},
        "counts": counts, "output_sha256": sha, "outputs": [str(out)],
    }
    return records, manifest
