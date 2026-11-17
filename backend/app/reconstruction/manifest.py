"""Manifest — 阶段检查点与输入清单（断点续跑依据）。

每个成功阶段的输出目录写入 manifest.json：
{
  "stage": "...", "params_sha": "...", "inputs_sha": "...",
  "files": [{"path": "相对路径", "sha256": "...", "size": n}],
  "completed_at": "..."
}
恢复时校验：params/inputs 哈希一致 + 文件存在 + 校验和一致 → 该阶段可跳过。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def compute_sha_of(obj) -> str:
    """对可 JSON 化对象计算稳定哈希。"""
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def write_manifest(stage_dir: str | Path, stage: str, params: dict, inputs: dict, files: list[str]) -> dict:
    stage_dir = Path(stage_dir)
    file_entries = []
    for entry in files:
        p = Path(entry)
        if not p.is_absolute():
            p = stage_dir / entry
        if p.is_file():
            file_entries.append({"path": str(p), "sha256": sha256_file(p), "size": p.stat().st_size})
    manifest = {
        "stage": stage,
        "params_sha": compute_sha_of(params),
        "inputs_sha": compute_sha_of(inputs),
        "params": params,
        "files": file_entries,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    out = stage_dir / "manifest.json"
    fd, tmp = tempfile.mkstemp(dir=str(stage_dir), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out)  # 原子替换，避免半成品
    return manifest


def read_manifest(stage_dir: str | Path) -> dict | None:
    p = Path(stage_dir) / "manifest.json"
    if not p.is_file():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def stage_checkpoint_valid(stage_dir: str | Path, stage: str, params: dict, inputs: dict) -> bool:
    """校验检查点是否可复用：参数/输入一致 + 文件齐全且校验一致。"""
    m = read_manifest(stage_dir)
    if not m or m.get("stage") != stage:
        return False
    if m.get("params_sha") != compute_sha_of(params):
        return False
    if m.get("inputs_sha") != compute_sha_of(inputs):
        return False
    files = m.get("files") or []
    if not files:
        return False
    for fe in files:
        p = Path(fe["path"])  # v2: 绝对路径；v1 兼容：相对 stage_dir
        if not p.is_absolute():
            p = Path(stage_dir) / fe["path"]
        if not p.is_file() or p.stat().st_size != fe.get("size"):
            return False
    return True
