"""应用核心配置 — 所有路径/地址通过环境变量管理，不硬编码。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录（backend/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("RECON_ENV_FILE", str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 应用
    app_name: str = "图片三维重建系统"
    api_v1_prefix: str = "/api/v1"
    secret_key: str = "dev-secret-change-me"
    access_token_expire_minutes: int = 1440
    log_level: str = "INFO"

    # 数据库
    database_url: str = "sqlite:////var/lib/recon-data/recon.db"

    # 队列
    redis_url: str = "redis://127.0.0.1:6379/0"

    # 文件存储
    data_dir: str = str(PROJECT_ROOT / "data")
    scratch_dir: str = "/var/lib/recon-data/scratch"

    # 上传限制
    max_upload_file_mb: float = 200
    max_project_total_mb: float = 2048
    max_project_images: int = 300
    allowed_image_extensions: str = "jpg,jpeg,png,tif,tiff,bmp,webp"

    # 重建引擎
    recon_mode: str = "colmap"  # colmap | mock
    colmap_bin: str = "colmap"
    recon_use_gpu: str = "auto"  # auto | 1 | 0
    default_recon_preset: str = "standard"

    # CORS
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---- 派生属性 ----
    @property
    def allowed_exts(self) -> list[str]:
        return [e.strip().lower().lstrip(".") for e in self.allowed_image_extensions.split(",") if e.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def use_gpu(self) -> bool:
        if self.recon_use_gpu == "1":
            return True
        if self.recon_use_gpu == "0":
            return False
        return _detect_cuda()  # auto

    def ensure_dirs(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.scratch_dir).mkdir(parents=True, exist_ok=True)


def _detect_cuda() -> bool:
    """检测 COLMAP 是否为 CUDA 构建（colmap -h 输出包含 'CUDA'）。"""
    import shutil
    import subprocess

    from app.core.config import get_settings

    bin_path = shutil.which(get_settings().colmap_bin)
    if not bin_path:
        return False
    try:
        out = subprocess.run(
            [bin_path, "-h"], capture_output=True, text=True, timeout=15
        )
        return "CUDA" in (out.stdout + out.stderr)
    except Exception:
        return False


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
