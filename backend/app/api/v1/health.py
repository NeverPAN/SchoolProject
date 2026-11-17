"""健康检查 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    settings = get_settings()
    from app.reconstruction.adapter import get_colmap_version

    colmap_version = get_colmap_version(settings.colmap_bin) if settings.recon_mode == "colmap" else None
    return {
        "status": "ok",
        "app": settings.app_name,
        "recon_mode": settings.recon_mode,
        "colmap_available": colmap_version is not None,
        "colmap_version": colmap_version,
        "use_gpu": settings.use_gpu if settings.recon_mode == "colmap" else False,
    }


@router.get("/health/db")
def health_db(db=Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}


@router.get("/health/worker")
def health_worker():
    settings = get_settings()
    try:
        import redis
        from rq import Queue

        conn = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        conn.ping()
        q = Queue("recon", connection=conn)
        return {
            "status": "ok",
            "redis": "connected",
            "queued_jobs": len(q.jobs),
        }
    except Exception as e:
        return {"status": "degraded", "redis": f"unavailable: {e}"}
