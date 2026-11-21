"""认证 API — 注册/登录/退出/当前用户。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ConflictError, UnauthorizedError
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.entities import User
from app.schemas.api import LoginRequest, RegisterRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])
router_root = APIRouter(tags=["auth"])  # /api/v1 级别的端点（me）


@router.post("/register", response_model=UserOut, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    exists = db.scalar(select(User).where(User.username == body.username))
    if exists:
        raise ConflictError("用户名已存在")
    user = User(username=body.username, password_hash=hash_password(body.password), role="user")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    if not user or not verify_password(body.password, user.password_hash):
        raise UnauthorizedError("用户名或密码错误")
    token = create_access_token(user.id, user.username, user.role)
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.post("/logout", response_model=UserOut)
def logout(user: User = Depends(get_current_user)):
    # JWT 无状态：由前端丢弃 Token；预留服务端黑名单扩展点
    return UserOut.model_validate(user)


@router_root.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)
