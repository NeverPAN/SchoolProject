"""SQLAlchemy 2.x 声明式基类与通用 mixin。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """带时区的当前 UTC 时间（用于 JSON/manifest 字符串）。"""
    return datetime.now(timezone.utc)


def utcnow_db() -> datetime:
    """数据库存储用 naive UTC（SQLite DateTime 列不带时区，统一约定避免混用）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


__all__ = ["Base", "TimestampMixin", "utcnow"]
