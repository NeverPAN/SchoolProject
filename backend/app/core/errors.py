"""统一错误体系 — 稳定错误码 + 可读信息。"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """业务异常基类：HTTP 状态码 + 稳定错误码 + 可读信息。"""

    status_code = 400
    code = "APP_ERROR"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        super().__init__(message)


class UnauthorizedError(AppError):
    status_code = 401
    code = "UNAUTHORIZED"


class ForbiddenError(AppError):
    status_code = 403
    code = "FORBIDDEN"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"


class ValidationError(AppError):
    status_code = 400
    code = "VALIDATION_ERROR"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "PAYLOAD_TOO_LARGE"


def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )


def generic_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # 不向前端泄露内部堆栈，仅记录服务端日志
    import traceback

    from app.core.logging_setup import get_logger

    get_logger().error("未处理异常: %s\n%s", exc, traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"code": "INTERNAL_ERROR", "message": "服务器内部错误，请查看服务端日志"},
    )
