"""文件工具 — 安全路径、流式写入、校验和。"""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

_FILENAME_SAFE = re.compile(r"[^\w.\-()（）\u4e00-\u9fff]+", re.UNICODE)


def sanitize_filename(name: str) -> str:
    """清理用户文件名（仅用于展示与保留原始名，存储名由 UUID 生成）。"""
    name = Path(name).name  # 防路径穿越
    name = _FILENAME_SAFE.sub("_", name).strip("._ ")
    return name[:180] or "unnamed"


def safe_relpath(*parts: str | int) -> Path:
    """拼接相对路径并校验无穿越（片段不得包含 ..）。"""
    parts_str = [str(p) for p in parts]
    for p in parts_str:
        if ".." in Path(p).parts or p.startswith(("/", "\\")):
            raise ValueError(f"非法路径片段: {p}")
    return Path(*parts_str)


def sha256_of_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_stream(stream) -> str:
    """从 file-like 对象计算 sha256（不改变读取位置需调用方自行 seek）。"""
    h = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        h.update(chunk)
    return h.hexdigest()


def new_storage_name(ext: str) -> str:
    """UUID 存储名，不信任用户路径。"""
    return f"{uuid.uuid4().hex}{ext}"


def human_size(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"
