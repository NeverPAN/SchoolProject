"""API 依赖 — 认证与归属校验。"""
from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.errors import UnauthorizedError
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.entities import Project, User


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise UnauthorizedError("未登录或缺少 Token")
    token = auth[len("Bearer "):].strip()
    payload = decode_access_token(token)
    if not payload:
        raise UnauthorizedError("Token 无效或已过期")
    user_id = int(payload["sub"])
    user = db.get(User, user_id)
    if not user:
        raise UnauthorizedError("用户不存在")
    return user


def get_owned_project(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Project:
    from app.core.errors import NotFoundError

    project = db.get(Project, project_id)
    if not project or project.owner_id != user.id:
        raise NotFoundError("项目不存在")
    return project
