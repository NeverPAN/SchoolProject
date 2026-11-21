"""Pydantic schemas — 请求/响应数据契约（docs/api-contract.md 的实现）。"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, FiniteFloat, field_validator, model_validator

# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


class ApiResponse(BaseModel):
    code: str = "OK"
    message: str = "success"


class Paginated(BaseModel):
    total: int
    items: list[Any]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)

    @field_validator("username")
    @classmethod
    def username_safe(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"[\w\u4e00-\u9fff.-]+", v):
            raise ValueError("用户名只能包含字母、数字、下划线、中文、点、横线")
        return v


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)


class ImageBrief(BaseModel):
    id: int
    original_filename: str
    file_size: int
    width: int
    height: int
    format: str
    quality_status: str
    has_thumbnail: bool

    model_config = {"from_attributes": True}


class ProjectOut(BaseModel):
    id: int
    name: str
    description: str
    status: str
    owner_id: int
    created_at: datetime
    updated_at: datetime
    image_count: int = 0
    total_image_bytes: int = 0
    model_count: int = 0
    job_count: int = 0

    model_config = {"from_attributes": True}


class ProjectDetailOut(ProjectOut):
    images: list[ImageBrief] = []
    quality_summary: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------


class ImageOut(ImageBrief):
    id: int
    project_id: int
    stored_name: str
    checksum: str
    exif: dict[str, Any] | None = None
    quality_notes: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ImageDeleteResult(BaseModel):
    code: str = "OK"
    message: str
    image_id: int


class QualityCheckSummary(BaseModel):
    checked: int
    ok: int
    warning: int
    invalid: int


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

ReconPreset = Literal["fast", "standard", "high", "speed"]
MatcherType = Literal["auto", "exhaustive", "sequential"]


class JobCreateRequest(BaseModel):
    preset: ReconPreset = "standard"
    matcher: MatcherType = "auto"
    enable_dense: bool = True
    enable_mesh: bool = True
    experiment_name: str | None = Field(default=None, max_length=120)


class JobConfigOut(BaseModel):
    preset: ReconPreset
    matcher: MatcherType
    enable_dense: bool
    enable_mesh: bool
    use_gpu: bool
    mode: str  # colmap | mock
    # A1 冻结解析：任务创建时确定的执行参数、提交预检与 GPU 能力报告
    resolved: dict | None = None
    preflight: dict | None = None
    capability: dict | None = None
    matcher_decision: str | None = None


class JobStageEventOut(BaseModel):
    id: int
    stage: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    message: str | None

    model_config = {"from_attributes": True}


class JobOut(BaseModel):
    id: int
    project_id: int
    status: str
    current_stage: str | None
    progress: float
    pipeline_mode: str
    experiment_name: str | None
    engine_version: str | None
    config: JobConfigOut
    started_at: datetime | None
    finished_at: datetime | None
    error_stage: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class JobDetailOut(JobOut):
    events: list[JobStageEventOut] = []


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class ModelOut(BaseModel):
    id: int
    project_id: int
    job_id: int | None
    model_type: str
    format: str
    file_size: int
    vertex_count: int
    face_count: int
    point_count: int
    stage_origin: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


_FiniteCoordinate = Annotated[FiniteFloat, Field(strict=True)]
_ProvenanceId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class LocalExtractionBounds(BaseModel):
    min: tuple[_FiniteCoordinate, _FiniteCoordinate, _FiniteCoordinate]
    max: tuple[_FiniteCoordinate, _FiniteCoordinate, _FiniteCoordinate]

    @model_validator(mode="after")
    def ordered_bounds(self) -> LocalExtractionBounds:
        if not all(lo < hi for lo, hi in zip(self.min, self.max)):
            raise ValueError("Each maximum must exceed its minimum")
        return self


class LocalExtractionOut(BaseModel):
    parent_model_id: _ProvenanceId
    region_id: _ProvenanceId
    bounds: LocalExtractionBounds


class ModelDetailOut(ModelOut):
    local_extraction: LocalExtractionOut | None = None
    local_reconstruction_available: bool = False
    local_reconstruction_mode: str | None = None
    local_reconstruction_image_ids: list[int] = Field(default_factory=list)
    local_reconstruction_bounds: dict[str, list[float]] | None = None
    experiment_name: str | None = None
    experiment: dict | None = None
    pipeline_mode: str | None = None
    versions: list["ModelVersionOut"] = []
    display_up: tuple[float, float, float] | None = None
    texture_assets: dict | None = None
    legacy_srgb_vertex_colors: bool = False


class ModelVersionOut(BaseModel):
    id: int
    version_number: int
    parent_version_id: int | None
    change_type: str
    change_description: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ModelVersionCreate(BaseModel):
    model_id: int
    change_type: Literal["initial", "append_images", "manual"] = "manual"
    change_description: str = Field(default="", max_length=2000)


class ModelVersionOutWithModel(ModelVersionOut):
    model: ModelOut


class ModelRegionIn(BaseModel):
    """阶段 8 原型：查看器框选的三维包围盒（模型坐标系）。"""
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    camera: dict = Field(default_factory=dict)  # 框选时的相机参数（可复现）
    note: str = Field(default="", max_length=255)


class ModelRegionOut(BaseModel):
    id: int
    model_id: int
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    note: str
    created_at: datetime

    model_config = {"from_attributes": True}


class LocalUpdateIn(BaseModel):
    region_id: int


class LocalReconstructionIn(BaseModel):
    """局部重建：通用模式 1–32 张任意新增照片；受试种子模式仍要求恰好 3 张。"""
    region_id: int
    image_ids: list[int] = Field(min_length=1, max_length=32)
