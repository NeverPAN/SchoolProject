"""任务服务 — 创建/取消/恢复/查询（数据库为任务状态权威来源）。"""
from __future__ import annotations

import json
import math
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
    ModelRegion,
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


def _preflight_images(images: list[Image]) -> dict:
    """A1 提交预检：文件可读性、分辨率摘要与提示性质量汇总。

    视角重叠只能在特征注册后判定，这里如实标注 unknown，不用代理冒充结论。
    """
    missing = [img.original_filename for img in images if not Path(img.stored_path).is_file()]
    sizes = [(img.width, img.height) for img in images if img.width > 0 and img.height > 0]
    quality: dict[str, int] = {}
    for img in images:
        quality[img.quality_status] = quality.get(img.quality_status, 0) + 1
    warnings = []
    if quality.get("warning"):
        warnings.append(f"{quality['warning']} 张图片质量检查有警告（低分辨率/模糊/曝光），详见图片管理页")
    if quality.get("invalid"):
        warnings.append(f"{quality['invalid']} 张图片质量检查失败，可能无法参与重建")
    small = [name for (img, (w, h)) in zip(images, sizes) for name in [img.original_filename] if w < 640 or h < 480]
    if small:
        warnings.append(f"{len(small)} 张图片分辨率低于 640x480：{'、'.join(small[:5])}{'…' if len(small) > 5 else ''}")
    return {
        "image_count": len(images),
        "total_bytes": sum(img.file_size for img in images),
        "min_resolution": min(sizes) if sizes else None,
        "max_resolution": max(sizes) if sizes else None,
        "quality_summary": quality,
        "missing_files": missing,
        "warnings": warnings,
        "view_overlap": "unknown_until_registration",
    }


def create_job(
    db: Session,
    project,
    preset: str,
    matcher: str,
    enable_dense: bool,
    enable_mesh: bool,
    *, status: str = "CREATED", experiment_name: str | None = None,
) -> ReconstructionJob:
    mode = _resolve_mode(None)
    _lock_project(db, project.id)
    if _other_active(db, project.id):
        raise ConflictError("该项目已有进行中的重建任务，请等待其结束")
    images = list(db.scalars(select(Image).where(Image.project_id == project.id).order_by(Image.id)))
    if len(images) < 3:
        raise ValidationError("项目图片不足 3 张，无法重建")

    # A1 提交预检：缺失/不可读文件在提交时直接失败，而不是排队后才报错。
    preflight = _preflight_images(images)
    if preflight["missing_files"]:
        raise ValidationError(
            f"输入文件缺失：{'、'.join(preflight['missing_files'][:5])}，请重新上传后再提交任务",
            code="INPUT_FILES_MISSING",
        )

    # A1 配置冻结：preset/画像/环境一次解析，执行与恢复只读取这份结果。
    from app.reconstruction.profiles import gpu_capability_report, resolve_job_config

    frozen = resolve_job_config(preset, matcher, enable_dense, enable_mesh, mode,
                                filenames=[img.original_filename for img in images])
    resolved = frozen["resolved"]

    # A1 能力门槛：稠密需要 CUDA COLMAP；请求的每张 GPU 都必须真实可见。
    # 不再静默把稠密任务降级为稀疏成功。
    capability: dict | None = None
    if mode == "colmap" and enable_dense:
        capability = gpu_capability_report(resolved["colmap_bin"])
        if not capability["colmap_has_cuda"]:
            raise ValidationError(
                "当前 COLMAP 为 CPU 构建（无 CUDA），无法执行稠密重建。"
                "请安装 CUDA 版 COLMAP，或改用不需要稠密的稀疏任务",
                code="CUDA_UNAVAILABLE",
            )
        if capability["all_requested_gpus_visible"] is False:
            raise ValidationError(
                f"GPU 配置不可用：COLMAP_GPU_INDEX={resolved['gpu_index']} 请求了 "
                f"{capability['requested_gpu_index']}，但本机仅可见 {capability['visible_gpu_count']} 张 GPU。"
                "请调整 COLMAP_GPU_INDEX 或更换执行环境",
                code="GPU_INDEX_UNAVAILABLE",
            )

    config = {
        "preset": preset,
        "matcher": matcher,
        "enable_dense": bool(enable_dense),
        "enable_mesh": bool(enable_mesh),
        "use_gpu": resolved["use_gpu"],
        "mode": mode,
        "resolved": resolved,
        "preflight": preflight,
        "capability": capability,
        "matcher_decision": frozen["matcher_decision"],
    }
    manifest = image_service.build_input_manifest(db, project)
    job = ReconstructionJob(
        project_id=project.id,
        status=status,
        config_json=json.dumps(config, ensure_ascii=False),
        experiment_name=(experiment_name or '').strip()[:120] or None,
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
    _enqueue_or_fail(db, job.id, "QUEUED")
    db.refresh(job)
    return job


def local_seed(model: Model) -> dict | None:
    path = get_settings().local_reconstruction_seed
    if path is None:
        return None
    try:
        seed = json.loads(path.read_text(encoding="utf-8"))
        if seed["target_model_id"] == model.id and seed["parent_model"]["path"] == model.file_path:
            return seed
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def general_local_available(model: Model) -> bool:
    """通用局部更新（阶段 D1/D2）可用性：父任务有稀疏检查点和融合点云。"""
    job = model.job
    if job is None:
        return False
    base = Path(image_service.project_job_dir(model.project, job.id)) / "checkpoints"
    return ((base / "SPARSE_RECONSTRUCTION" / "model" / "images.bin").is_file()
            and (base / "MESHING" / "fused-filtered.ply").is_file())


def _freeze_local_general(db: Session, model: Model, region: ModelRegion,
                          selected: list[dict]) -> dict:
    """D1：为通用局部更新冻结父模型、父稀疏模型、父点云、选区与新增照片。"""
    from app.reconstruction.local_general import MAX_NEW_IMAGES
    from app.reconstruction.local_reconstruction import GUARD, SPARSE_FILES

    job = model.job
    if job is None:
        raise ValidationError("该模型缺少来源任务，无法冻结父稀疏模型")
    base = Path(image_service.project_job_dir(model.project, job.id)) / "checkpoints"
    sparse_dir = base / "SPARSE_RECONSTRUCTION" / "model"
    parent_database = base / "FEATURE_EXTRACTION" / "database.db"
    if not parent_database.is_file():
        raise ValidationError("父模型缺少特征数据库检查点（database.db），无法注册新增照片")
    sparse_files = {}
    for name in (*SPARSE_FILES, "rigs.bin", "frames.bin"):
        path = sparse_dir / name
        if path.is_file():
            sparse_files[name] = {"path": str(path), "sha256": _sha256_file(path)}
    if not SPARSE_FILES.issubset(sparse_files):
        raise ValidationError("父模型缺少稀疏重建检查点，无法进行局部重建")

    from app.reconstruction.local_reconstruction import _poses

    try:
        model_names = set(_poses(sparse_dir / "images.bin"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValidationError("父稀疏模型损坏或不可读") from exc
    parent_manifest = json.loads(job.input_manifest or "{}").get("images", [])
    parent_images = []
    for index, entry in enumerate(parent_manifest):
        name = f"img_{index:04d}_{_sanitize_stem(entry['filename'])}{Path(entry['filename']).suffix.lower()}"
        if name not in model_names:
            raise ValidationError("父稀疏模型与父任务输入清单不一致，无法冻结局部更新")
        parent_images.append({"name": name, "path": str(Path(entry["stored_path"]).resolve()),
                              "sha256": entry["checksum"]})

    cloud_path = Path(image_service.project_job_dir(model.project, job.id)) / "checkpoints" / "MESHING" / "fused-filtered.ply"
    if not cloud_path.is_file():
        raise ValidationError("父模型缺少融合点云检查点（fused-filtered.ply），无法进行局部点云融合")
    if not 1 <= len(selected) <= MAX_NEW_IMAGES:
        raise ValidationError(f"局部更新支持 1–{MAX_NEW_IMAGES} 张新增照片")
    lo = [region.bbox_min_x, region.bbox_min_y, region.bbox_min_z]
    hi = [region.bbox_max_x, region.bbox_max_y, region.bbox_max_z]
    if not all(math.isfinite(a) and math.isfinite(b) and b - a > 2 * GUARD for a, b in zip(lo, hi, strict=True)):
        raise ValidationError("选区过小或无效：每个维度必须大于 2×保护带（0.06 模型单位）")
    settings = get_settings()
    resolved = {
        "max_features": 8192,
        "dense_max_image_size": 1600,
        "patchmatch_iterations": settings.colmap_patchmatch_iterations,
        "gpu_index": settings.colmap_gpu_index,
        "fusion_threads": settings.colmap_fusion_threads,
        "use_gpu": True,
    }
    return {
        "parent_model_id": model.id,
        "parent_model": {"path": str(Path(model.file_path).resolve()), "sha256": _sha256_file(Path(model.file_path))},
        "parent_cloud": {"path": str(cloud_path)},
        "parent_database": {"path": str(parent_database), "sha256": _sha256_file(parent_database)},
        "parent_sparse": sparse_files,
        "parent_images": parent_images,
        "bounds": {"min": lo, "max": hi},
        "resolved": resolved,
    }


def _job_of(model: Model):
    return model.job


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_stem(filename: str) -> str:
    from app.utils.files import sanitize_filename

    return sanitize_filename(Path(filename).stem)


def submit_local_reconstruction(db: Session, model: Model, region: ModelRegion, image_ids: list[int]):
    _lock_project(db, model.project_id)
    if _other_active(db, model.project_id):
        raise ConflictError("该项目已有进行中的任务")
    if region.model_id != model.id or region.user_id != model.project.owner_id:
        raise NotFoundError("选区不存在")
    seed = local_seed(model)
    entries = image_service.build_input_manifest(db, model.project)["images"]
    selected = [entry for entry in entries if entry["image_id"] in image_ids]
    if seed is not None:
        lo = [region.bbox_min_x, region.bbox_min_y, region.bbox_min_z]
        hi = [region.bbox_max_x, region.bbox_max_y, region.bbox_max_z]
        if not all(math.isfinite(a) and math.isfinite(b) and l <= a < b <= h and b-a > 0.06
                   for a,b,l,h in zip(lo,hi,seed["bbox_min"],seed["bbox_max"], strict=True)):
            raise ValidationError("选区不在已验证范围内，请使用已验证选区")
        by_sha = {entry["checksum"]:entry["image_id"] for entry in selected}
        expected = seed["new_view_sha256"]
        if len(selected) != 3 or len(set(image_ids)) != 3 or set(by_sha) != set(expected.values()):
            raise ValidationError("请在同一项目上传并选择已校验的 3 张原片 DSC_1290、1291、1292")
        seed["new_views"] = {name:by_sha[sha] for name,sha in expected.items()}
        mode = "local_reconstruct"
        config = dict(mode=mode, preset="standard", matcher="exhaustive", use_gpu=True,
                      enable_dense=True, enable_mesh=False, region_id=region.id,
                      target_model_id=model.id, bounds={"min":lo,"max":hi}, seed=seed)
        experiment_name = f"新增照片局部点云 · #{model.id}"
    else:
        # 通用路径（阶段 D1/D2）：任意新增照片 1–32 张，注册到父稀疏模型
        mode = "local_general"
        config = _freeze_local_general(db, model, region, selected)
        config.update(mode=mode, preset="standard", matcher="exhaustive", region_id=region.id,
                      use_gpu=True, enable_dense=True, enable_mesh=False)
        experiment_name = f"局部更新 · 新增 {len(selected)} 张照片 · #{model.id}"
    job = ReconstructionJob(project_id=model.project_id, status="QUEUED", pipeline_mode=mode,
                            experiment_name=experiment_name, config_json=json.dumps(config),
                            input_manifest=json.dumps({"images": [dict(e, stored_path=str(Path(e["stored_path"]).resolve()))
                                                                   for e in selected],
                                                       "image_count": len(selected)}))
    db.add(job)
    db.commit()
    _enqueue_or_fail(db, job.id, "QUEUED")
    db.refresh(job)
    return job


def submit_local_mesh(db: Session, model: Model):
    """阶段 D3/D4：以局部融合点云候选为源，生成父坐标系的网格补丁候选。"""
    _lock_project(db, model.project_id)
    if _other_active(db, model.project_id):
        raise ConflictError("该项目已有进行中的任务")
    if model.stage_origin != "LOCAL_POINT_FUSION" or model.format != "ply":
        raise ValidationError("局部网格补丁必须以局部融合点云候选为源")
    manifest_path = Path(model.file_path).parent / "job_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationError("局部融合点云缺少发布清单，无法确定父版本与选区") from exc
    if manifest.get("job_id") != model.job_id or manifest.get("pipeline_mode") not in ("local_general", "local_reconstruct"):
        raise ValidationError("局部融合点云清单与模型不匹配")
    parent_id, region_id = manifest.get("parent_model_id"), manifest.get("region_id")
    if type(parent_id) is not int or type(region_id) is not int:
        raise ValidationError("局部融合点云清单缺少父版本或选区")
    parent = db.get(Model, parent_id)
    region = db.get(ModelRegion, region_id)
    if parent is None or parent.project_id != model.project_id:
        raise ValidationError("父版本不存在或不在同一项目")
    if region is None or region.model_id != parent_id or region.user_id != model.project.owner_id:
        raise ValidationError("选区不存在或不属于父版本")
    if parent.format != "glb" or not Path(parent.file_path).is_file():
        raise ValidationError("父版本必须是可读取的 GLB 网格")
    source_cloud = Path(model.file_path)
    if not source_cloud.is_file():
        raise ValidationError("局部融合点云文件缺失")
    # D3 产品决策：低 trim 填洞。local_mesh_patch.py 会按同一常量执行，
    # 这里也必须冻结为 4，避免任务详情与实际 Worker 参数不一致。
    resolved = {"poisson_depth": 8, "poisson_trim": 4, "poisson_threads": 4}
    config = {
        "mode": "local_mesh", "region_id": region.id,
        "parent_model_id": parent.id,
        "parent_model": {"path": str(Path(parent.file_path).resolve()), "sha256": _sha256_file(Path(parent.file_path))},
        "source_model_id": model.id,
        "source_cloud": {"path": str(source_cloud.resolve()), "sha256": _sha256_file(source_cloud)},
        "bounds": {"min": manifest["resolved_params"]["bbox_min"], "max": manifest["resolved_params"]["bbox_max"]},
        "resolved": resolved, "preset": "standard", "matcher": "exhaustive",
        "use_gpu": False, "enable_dense": False, "enable_mesh": False,
    }
    if parent.stage_origin == "PHOTO_TEXTURE_UPRIGHT":
        # D3 需要原生坐标父网格：烘焙 GLB 的正立旋转必须可逆且可核对
        manifest_path = Path(parent.file_path).parent / "job_manifest.json"
        try:
            parent_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValidationError("父版本清单缺失，无法恢复原生坐标") from exc
        up = parent_manifest.get("display_up")
        if not (isinstance(up, list) and len(up) == 3 and all(isinstance(v, (int, float)) for v in up)):
            raise ValidationError("父版本清单缺少 display_up，无法恢复原生坐标")
        base = Path(image_service.project_job_dir(model.project, _job_of(parent).id)) / "checkpoints" / "MESHING"
        native = base / "parent-native-source.ply"
        if not native.is_file():
            native = base / "meshed-poisson.ply"
        if not native.is_file():
            raise ValidationError("父版本缺少原生网格检查点（MESHING），无法恢复原生坐标")
        import trimesh as _trimesh

        native_mesh = _trimesh.load(native, force="mesh", process=False)
        center = native_mesh.bounds.mean(axis=0).tolist()
        config["parent_native_mesh"] = {"path": str(native.resolve()), "sha256": _sha256_file(native)}
        config["parent_display"] = {"up": [float(v) for v in up], "center": center}
    job = ReconstructionJob(project_id=model.project_id, status="QUEUED", pipeline_mode="local_mesh",
                            experiment_name=f"局部网格补丁 · 源 #{model.id} · 父 #{parent.id}",
                            config_json=json.dumps(config), input_manifest="{}")
    db.add(job)
    db.commit()
    _enqueue_or_fail(db, job.id, "QUEUED")
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


def submit_new_job(db: Session, project, preset: str, matcher: str, enable_dense: bool, enable_mesh: bool, experiment_name: str | None = None) -> ReconstructionJob:
    job = create_job(db, project, preset, matcher, enable_dense, enable_mesh, status="QUEUED", experiment_name=experiment_name)
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
    if job.pipeline_mode in ("local_reconstruct", "local_general"):
        raise ConflictError("局部重建任务不支持恢复，请提交新任务")
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
