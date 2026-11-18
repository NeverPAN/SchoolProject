"""Atomic, content-verified stage checkpoints."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def compute_sha_of(obj) -> str:
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def input_manifest_sha(manifest: dict) -> str:
    return compute_sha_of({k: v for k, v in manifest.items() if k != "generated_at"})


def atomic_write_json(path: Path, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def write_manifest(stage_dir: str | Path, stage: str, params: dict, inputs: dict,
                   files: Sequence[str | Path]) -> dict:
    root = Path(stage_dir).resolve()
    entries = []
    for p in sorted({(root / entry).resolve() for entry in files}):
        if not p.is_file() or p.stat().st_size <= 0:
            raise ValueError(f"Missing or empty checkpoint output: {p}")
        path = str(p.relative_to(root)) if p.is_relative_to(root) else str(p)
        entries.append({"path": path, "sha256": sha256_file(p), "size": p.stat().st_size})
    if not entries:
        raise ValueError(f"No checkpoint outputs for {stage}")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "params_sha": compute_sha_of(params),
        "inputs_sha": compute_sha_of(inputs),
        "params": params,
        "files": entries,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(root / "manifest.json", manifest)
    return manifest


def checkpoint_fingerprint(manifest: dict) -> str:
    return compute_sha_of({k: v for k, v in manifest.items() if k != "completed_at"})


def read_manifest(stage_dir: str | Path) -> dict | None:
    try:
        with open(Path(stage_dir) / "manifest.json", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        return None


def manifest_files_valid(stage_dir: str | Path, manifest: dict,
                         required: Sequence[str | Path] = ()) -> bool:
    root = Path(stage_dir).resolve()
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return False
    seen = set()
    try:
        for entry in files:
            p = (root / entry["path"]).resolve()
            stat = p.stat()
            if (p in seen or not p.is_file() or stat.st_size <= 0
                    or stat.st_size != entry["size"] or sha256_file(p) != entry["sha256"]):
                return False
            seen.add(p)
        return all((root / name).resolve() in seen for name in required)
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        return False


def checked_manifest(stage_dir: str | Path, stage: str, params: dict, inputs: dict,
                     required: Sequence[str | Path] = ()) -> dict | None:
    m = read_manifest(stage_dir)
    if not m or m.get("schema_version") != SCHEMA_VERSION or m.get("stage") != stage:
        return None
    if (m.get("params_sha") != compute_sha_of(params)
            or m.get("inputs_sha") != compute_sha_of(inputs)):
        return None
    return m if manifest_files_valid(stage_dir, m, required) else None


def stage_checkpoint_valid(stage_dir: str | Path, stage: str, params: dict, inputs: dict,
                           required: Sequence[str | Path] = ()) -> bool:
    return checked_manifest(stage_dir, stage, params, inputs, required) is not None
