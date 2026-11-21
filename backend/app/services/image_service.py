"""图片服务 — 流式上传、元数据、缩略图、质量检查。

对应指南模块 B（图片管理）与模块 C（图像处理）：
- 流式写入，不将整个上传文件一次性读入内存；
- 文件名/存储名分离，存储名由 UUID 生成，防路径穿越；
- 记录分辨率、格式、EXIF、SHA-256；
- 质量检查给出提示性结论（模糊/过曝/欠曝/过小），不保证判断绝对准确。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps
from PIL.ExifTags import TAGS
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, PayloadTooLargeError, ValidationError
from app.models.entities import Image, Project
from app.utils.files import new_storage_name, sanitize_filename

THUMBNAIL_SIZE = (320, 320)


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def project_input_dir(project: Project) -> Path:
    settings = get_settings()
    d = Path(settings.data_dir) / "users" / str(project.owner_id) / "projects" / str(project.id) / "input" / "original"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_thumb_dir(project: Project) -> Path:
    settings = get_settings()
    d = Path(settings.data_dir) / "users" / str(project.owner_id) / "projects" / str(project.id) / "input" / "thumbnails"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_job_dir(project: Project, job_id: int) -> Path:
    settings = get_settings()
    d = Path(settings.scratch_dir) / "users" / str(project.owner_id) / "projects" / str(project.id) / "jobs" / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_output_dir(project: Project, job_id: int) -> Path:
    settings = get_settings()
    d = Path(settings.data_dir) / "users" / str(project.owner_id) / "projects" / str(project.id) / "outputs" / f"job-{job_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------


def _iter_upload_stream(upload_file, chunk_size: int = 1024 * 1024):
    """流式读取上传文件（不一次性载入内存）。"""
    while True:
        chunk = upload_file.file.read(chunk_size)
        if not chunk:
            break
        yield chunk


def check_upload_allowed(db: Session, project: Project, filename: str, declared_size: int | None) -> None:
    settings = get_settings()
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in settings.allowed_exts:
        raise ValidationError(f"不支持的文件格式 .{ext}，允许: {', '.join(settings.allowed_exts)}")
    if declared_size and declared_size > settings.max_upload_file_mb * 1024 * 1024:
        raise PayloadTooLargeError(
            f"单文件超过限制 {settings.max_upload_file_mb} MB"
        )
    count = db.scalar(select(func.count(Image.id)).where(Image.project_id == project.id)) or 0
    if count >= settings.max_project_images:
        raise ConflictError(f"项目图片数量已达上限 {settings.max_project_images}")


def save_uploaded_image(db: Session, project: Project, upload_file) -> Image:
    """保存一张上传图片：流式写盘 → 校验 → 元数据 → 缩略图 → 记录。"""
    settings = get_settings()
    original_name = sanitize_filename(upload_file.filename or "unnamed")
    check_upload_allowed(db, project, original_name, getattr(upload_file, "size", None))

    max_bytes = int(settings.max_upload_file_mb * 1024 * 1024)
    in_dir = project_input_dir(project)
    stored_name = new_storage_name(Path(original_name).suffix.lower())
    target = in_dir / stored_name

    # 流式写入 + 计算校验和 + 限制大小
    import hashlib

    h = hashlib.sha256()
    size = 0
    try:
        with open(target, "wb") as out:
            for chunk in _iter_upload_stream(upload_file):
                size += len(chunk)
                if size > max_bytes:
                    raise PayloadTooLargeError(f"单文件超过限制 {settings.max_upload_file_mb} MB")
                h.update(chunk)
                out.write(chunk)
        checksum = h.hexdigest()

        # 项目总大小限制
        total = db.scalar(
            select(func.coalesce(func.sum(Image.file_size), 0)).where(Image.project_id == project.id)
        ) or 0
        if total + size > settings.max_project_total_mb * 1024 * 1024:
            raise PayloadTooLargeError(f"项目总大小超过限制 {settings.max_project_total_mb} MB")

        # 项目内重复校验（同一 checksum 拒绝重复上传）
        dup = db.scalar(
            select(Image).where(Image.project_id == project.id, Image.checksum == checksum)
        )
        if dup:
            raise ConflictError(f"图片与已有文件内容重复（{dup.original_filename}）")

        # 用 PIL 验证真实可读 + 提取元数据
        meta = extract_metadata(target)
    except Exception:
        target.unlink(missing_ok=True)
        raise

    img = Image(
        project_id=project.id,
        original_filename=original_name,
        stored_path=str(target),
        file_size=size,
        width=meta["width"],
        height=meta["height"],
        format=meta["format"],
        checksum=checksum,
        exif_json=json.dumps(meta["exif"], ensure_ascii=False) if meta["exif"] else None,
        quality_status="pending",
    )
    db.add(img)
    db.commit()
    db.refresh(img)

    generate_thumbnail(img)
    db.commit()
    return img


# ---------------------------------------------------------------------------
# 元数据 / 缩略图 / 质量检查
# ---------------------------------------------------------------------------


def extract_metadata(path: str | Path) -> dict:
    try:
        with PILImage.open(path) as im:
            im.verify()  # 验证完整性
        with PILImage.open(path) as im:
            width, height = im.size
            fmt = (im.format or "").lower()
            exif_raw = im.getexif()
            exif = {}
            for tag_id, value in list(exif_raw.items())[:60]:
                tag = TAGS.get(tag_id, str(tag_id))
                try:
                    if isinstance(value, bytes):
                        value = value[:200].hex()
                    elif hasattr(value, "numerator") and hasattr(value, "denominator"):
                        value = float(value.numerator) / float(value.denominator)
                    else:
                        json.dumps(value)
                    exif[str(tag)] = value
                except (TypeError, ValueError):
                    continue
            # 拍摄时间与相机（若存在）
            keep = {}
            for k in ("Make", "Model", "DateTimeOriginal", "DateTime", "Software"):
                if k in exif:
                    keep[k] = exif[k]
        return {"width": width, "height": height, "format": fmt, "exif": keep}
    except Exception as e:
        raise ValidationError(f"无法读取图片（文件可能损坏）: {e}", code="INVALID_IMAGE") from e


def generate_thumbnail(img: Image) -> None:
    project = img.project
    thumb_dir = project_thumb_dir(project)
    out_path = thumb_dir / (Path(img.stored_path).stem + "_thumb.jpg")
    try:
        with PILImage.open(img.stored_path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail(THUMBNAIL_SIZE)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(out_path, "JPEG", quality=82)
        img.thumbnail_path = str(out_path)
    except Exception:
        img.thumbnail_path = None


def check_image_quality(img: Image) -> Image:
    """提示性质量检查：尺寸过小 / 模糊 / 过曝 / 欠曝。"""
    import cv2
    import numpy as np

    notes: list[str] = []
    status = "ok"
    try:
        pil = PILImage.open(img.stored_path)
        pil = ImageOps.exif_transpose(pil)
        arr = np.asarray(pil.convert("L"))

        if img.width < 640 or img.height < 480:
            notes.append("分辨率较低（<640x480），可能影响重建质量")
            status = "warning"

        scale = min(1.0, 1200 / max(arr.shape[0], arr.shape[1]))
        if scale < 1.0:
            arr = cv2.resize(arr, (0, 0), fx=scale, fy=scale)
        laplacian_var = cv2.Laplacian(arr, cv2.CV_64F).var()
        if laplacian_var < 60:
            notes.append(f"画面可能模糊（拉普拉斯方差 {laplacian_var:.1f} < 60）")
            status = "warning"

        mean_val = float(arr.mean())
        if mean_val > 230:
            notes.append(f"画面可能过曝（平均亮度 {mean_val:.0f} > 230）")
            status = "warning"
        elif mean_val < 25:
            notes.append(f"画面可能欠曝（平均亮度 {mean_val:.0f} < 25）")
            status = "warning"
    except Exception as e:
        notes.append(f"质量检查失败: {e}")
        status = "invalid"

    img.quality_status = status
    img.quality_notes = "; ".join(notes) if notes else None
    return img


def run_quality_check(db: Session, project: Project) -> dict:
    """对项目内 pending 的图片执行质量检查，返回摘要。"""
    stmt = select(Image).where(Image.project_id == project.id, Image.quality_status == "pending")
    images = list(db.scalars(stmt))
    for img in images:
        check_image_quality(img)
    db.commit()
    summary = {"checked": len(images), "ok": 0, "warning": 0, "invalid": 0}
    for img in images:
        summary[img.quality_status] = summary.get(img.quality_status, 0) + 1
    return summary


def delete_image(db: Session, img: Image) -> None:
    """删除图片文件与记录（保留缩略图清理）。"""
    for p in (img.stored_path, img.thumbnail_path):
        if p:
            Path(p).unlink(missing_ok=True)
    db.delete(img)
    db.commit()


def project_stats(db: Session, project: Project) -> dict:
    total_bytes = db.scalar(
        select(func.coalesce(func.sum(Image.file_size), 0)).where(Image.project_id == project.id)
    ) or 0
    count = db.scalar(select(func.count(Image.id)).where(Image.project_id == project.id)) or 0
    quality_rows = db.execute(
        select(Image.quality_status, func.count(Image.id))
        .where(Image.project_id == project.id)
        .group_by(Image.quality_status)
    ).all()
    quality = {row[0]: row[1] for row in quality_rows}
    return {"image_count": int(count), "total_image_bytes": int(total_bytes), "quality_summary": quality}


def build_input_manifest(db: Session, project: Project) -> dict:
    """输入清单：任务可追踪性依据（图片 id/文件名/校验和/分辨率）。"""
    images = list(db.scalars(select(Image).where(Image.project_id == project.id).order_by(Image.id)))
    entries = []
    for im in images:
        entries.append(
            {
                "image_id": im.id,
                "filename": im.original_filename,
                "stored_path": im.stored_path,
                "checksum": im.checksum,
                "width": im.width,
                "height": im.height,
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "image_count": len(entries),
        "images": entries,
    }
