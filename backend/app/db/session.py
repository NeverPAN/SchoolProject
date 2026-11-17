"""数据库引擎与会话管理。

SQLite（原型默认）：
  - WAL 模式 + busy_timeout，支持 FastAPI 线程池与 Worker 进程并发读写。
  - 连接放到本地磁盘（非 NFS），避免 NFS 文件锁问题（见 docs/environment-report.md）。
PostgreSQL：
  - 直接使用 DATABASE_URL，无需特殊处理。
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _make_engine() -> Engine:
    settings = get_settings()
    url = settings.database_url
    if url.startswith("sqlite"):
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)


engine = _make_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all() -> None:
    """建表（开发/测试用；正式版本用 Alembic 迁移）。"""
    from app.db.base import Base
    from app.models import entities  # noqa: F401 确保模型注册

    Base.metadata.create_all(engine)
