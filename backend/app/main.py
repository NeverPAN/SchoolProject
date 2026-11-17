"""FastAPI 应用入口。

- /api/v1 路由；统一错误结构；
- 生产模式托管 frontend/dist 静态资源（SPA fallback）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1 import auth, health, images, jobs, models, projects
from app.core.config import PROJECT_ROOT, get_settings
from app.core.errors import AppError, app_error_handler, generic_error_handler
from app.core.logging_setup import setup_logging

settings = get_settings()
setup_logging(settings.log_level, log_file=Path(settings.scratch_dir) / "logs" / "app.log")

app = FastAPI(title=settings.app_name, version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix=settings.api_v1_prefix)
app.include_router(auth.router, prefix=settings.api_v1_prefix)
app.include_router(auth.router_root, prefix=settings.api_v1_prefix)
app.include_router(projects.router, prefix=settings.api_v1_prefix)
app.include_router(images.router, prefix=settings.api_v1_prefix)
app.include_router(jobs.router, prefix=settings.api_v1_prefix)
app.include_router(models.router, prefix=settings.api_v1_prefix)

app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(Exception, generic_error_handler)

# ---- 前端静态托管（npm run build 后）----
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


@app.on_event("startup")
def _startup() -> None:
    settings.ensure_dirs()
    from app.db.session import create_all

    create_all()


if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
