"""ORM 模型 — users / projects / images / reconstruction_jobs / job_stage_events / models / model_versions。

设计对应 DEVELOPMENT_GUIDE.md §6.3，字段按第一阶段实现精简，
但保证 图片 → 任务 → 模型/版本 之间可追溯。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow_db

# ---------------------------------------------------------------------------
# 任务状态与阶段（与 docs/api-contract.md 保持一致）
# ---------------------------------------------------------------------------

JOB_STATUSES = [
    "CREATED",
    "VALIDATING",
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "RESUMING",
]

# 阶段顺序即执行顺序；权重用于估算进度（总和 100）
PIPELINE_STAGES: list[tuple[str, int]] = [
    ("IMAGE_VALIDATION", 2),
    ("FEATURE_EXTRACTION", 13),
    ("FEATURE_MATCHING", 15),
    ("SPARSE_RECONSTRUCTION", 15),
    ("IMAGE_UNDISTORTION", 5),
    ("DENSE_RECONSTRUCTION", 25),
    ("STEREO_FUSION", 10),
    ("MESHING", 8),
    ("TEXTURING", 2),
    ("EXPORTING", 5),
]
STAGE_NAMES = [s for s, _ in PIPELINE_STAGES]


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False)  # user | admin

    projects: Mapped[list["Project"]] = relationship(back_populates="owner")


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_db, onupdate=utcnow_db, nullable=False)

    owner: Mapped[User] = relationship(back_populates="projects")
    images: Mapped[list["Image"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["ReconstructionJob"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    models: Mapped[list["Model"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Image(Base, TimestampMixin):
    __tablename__ = "images"
    __table_args__ = (UniqueConstraint("project_id", "checksum", name="uq_project_checksum"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    width: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    format: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    exif_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    quality_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    project: Mapped[Project] = relationship(back_populates="images")


class ReconstructionJob(Base, TimestampMixin):
    __tablename__ = "reconstruction_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="CREATED", index=True, nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 阶段权重估算进度（0-100），明确标注为估算值
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    pipeline_mode: Mapped[str] = mapped_column(String(16), default="colmap", nullable=False)
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_manifest: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    project: Mapped[Project] = relationship(back_populates="jobs")
    stage_events: Mapped[list["JobStageEvent"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobStageEvent.id"
    )
    models: Mapped[list["Model"]] = relationship(back_populates="job")


class JobStageEvent(Base, TimestampMixin):
    __tablename__ = "job_stage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("reconstruction_jobs.id"), index=True, nullable=False
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # running|completed|failed|skipped|cancelled
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped[ReconstructionJob] = relationship(back_populates="stage_events")


class Model(Base, TimestampMixin):
    __tablename__ = "models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True, nullable=False)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("reconstruction_jobs.id"), nullable=True)
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)  # sparse_pointcloud|dense_pointcloud|mesh|web
    format: Mapped[str] = mapped_column(String(16), nullable=False)  # ply|glb|obj
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    vertex_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    face_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    point_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    stage_origin: Mapped[str | None] = mapped_column(String(64), nullable=True)

    project: Mapped[Project] = relationship(back_populates="models")
    job: Mapped[ReconstructionJob] = relationship(back_populates="models")
    versions: Mapped[list["ModelVersion"]] = relationship(back_populates="model")


class ModelVersion(Base, TimestampMixin):
    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True, nullable=False)
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id"), index=True, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_versions.id"), nullable=True
    )
    change_type: Mapped[str] = mapped_column(String(32), default="initial", nullable=False)  # initial|append_images|manual
    change_description: Mapped[str] = mapped_column(Text, default="")
    manifest_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    model: Mapped[Model] = relationship(back_populates="versions")


class ModelRegion(Base, TimestampMixin):
    """阶段 8 原型：查看器框选的三维区域（模型坐标系包围盒）。

    仅记录坐标并用于局部区域提取原型任务；不声称已实现局部重建。
    """
    __tablename__ = "model_regions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    bbox_min_x: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_min_y: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_min_z: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_max_x: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_max_y: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_max_z: Mapped[float] = mapped_column(Float, nullable=False)
    camera_json: Mapped[str] = mapped_column(Text, default="{}")  # 框选时的相机位姿（调试/复现用）
    note: Mapped[str] = mapped_column(String(255), default="")
