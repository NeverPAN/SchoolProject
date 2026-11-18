"""Optional CPU COLMAP texturing and conservative upright estimation."""
from __future__ import annotations

import re
import shutil
import struct
import subprocess
from pathlib import Path

import numpy as np

from app.reconstruction.adapter import ColmapAdapter

TEXTURING_OPTIONS = [
    "--MeshTextureMapping.num_threads", "8",
    "--MeshTextureMapping.texture_scale_factor", "0.5",
    "--MeshTextureMapping.atlas_patch_padding", "4",
]
ATLAS_SIDE_LIMIT = 8192
TEXTURE_SCALES = (0.5, 0.25, 0.125)
TEXTURING_TIMEOUT_SECONDS = 600
ORIENTATION_TIMEOUT_SECONDS = 180


class TexturingUnavailable(Exception):
    pass


def resolve_texturer(configured_bin: str) -> str:
    executable = shutil.which(configured_bin)
    if not executable:
        raise TexturingUnavailable("未找到 COLMAP_TEXTURER_BIN")
    return str(Path(executable).resolve())


def bake_texture(
    executable: str, workspace: Path, mesh: Path, output: Path, log: Path, cancel_check,
) -> Path:
    """Bake from an existing dense workspace; retry only oversized atlases."""
    cancel_check()
    probe = subprocess.run(
        [executable, "mesh_texturer", "-h", "--log_target", "stderr"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", timeout=10,
    )
    cancel_check()
    if probe.returncode or not all(option in probe.stdout for option in (
        "--workspace_path", "--input_path", "--output_path", *TEXTURING_OPTIONS[::2],
    )):
        raise TexturingUnavailable("配置的 COLMAP 不支持所需的 mesh_texturer 选项")
    if not (workspace / "images").is_dir() or not all(
        (workspace / "sparse" / f"{name}.bin").is_file() for name in ("cameras", "images", "points3D")
    ):
        raise TexturingUnavailable("去畸变照片工作区不完整")
    output.mkdir(parents=True, exist_ok=True)
    size_flag = TEXTURING_OPTIONS.index("--MeshTextureMapping.texture_scale_factor")
    for attempt, scale in enumerate(TEXTURE_SCALES):
        cancel_check()
        target = output / f"atlas-{attempt}"
        target.mkdir()
        options = list(TEXTURING_OPTIONS)
        options[size_flag + 1] = str(scale)
        stage_log = log.with_name(f"{log.stem}-attempt-{attempt + 1}{log.suffix}")
        ColmapAdapter(executable, use_gpu=False).run_command(
            ["mesh_texturer", "--workspace_path", str(workspace), "--input_path", str(mesh),
             "--output_path", str(target), "--log_target", "stderr", *options],
            stage_log, cancel_check, timeout_s=TEXTURING_TIMEOUT_SECONDS,
        )
        cancel_check()
        textured = target / "mesh.ply"
        atlas = target / "texture.png"
        if not all(p.is_file() and p.stat().st_size for p in (textured, atlas)):
            raise ValueError("照片贴图未生成完整网格和图集")
        with atlas.open("rb") as stream:
            header = stream.read(24)
        if len(header) != 24 or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR":
            raise ValueError("贴图工具未生成有效 PNG")
        width, height = struct.unpack(">II", header[16:])
        if not min(width, height) or max(width, height) > ATLAS_SIDE_LIMIT:
            if attempt + 1 == len(TEXTURE_SCALES):
                raise ValueError(f"照片图集超过尺寸上限：{width}×{height}")
            with stage_log.open("a", encoding="utf-8") as f:
                f.write(f"\natlas {width}x{height} exceeds limit; retrying scale {TEXTURE_SCALES[attempt + 1]}\n")
            continue
        destination = output / "selected"
        destination.mkdir(exist_ok=True)
        for p in (textured, atlas):
            shutil.copy2(p, destination / p.name)
        return destination / "mesh.ply"
    raise ValueError("照片图集尺寸重试失败")


def estimate_upright(
    executable: str, workspace: Path, scratch: Path, log: Path, camera_up: list[float] | None, cancel_check,
) -> list[float] | None:
    """A camera-roll hint disambiguates sign but is not itself scene gravity."""
    if camera_up is None:
        return None
    aligned = scratch / "aligned"
    aligned.mkdir()
    orientation_log = scratch / "orientation.log"
    try:
        ColmapAdapter(executable, use_gpu=False).run_command(
            ["model_orientation_aligner", "--input_path", str(workspace / "sparse"),
             "--output_path", str(aligned), "--image_path", str(workspace / "images"),
             "--method", "MANHATTAN-WORLD", "--log_target", "stderr"],
            orientation_log, cancel_check, timeout_s=ORIENTATION_TIMEOUT_SECONDS,
        )
        cancel_check()
        return _supported_up(orientation_log.read_text(encoding="utf-8"), camera_up)
    finally:
        if orientation_log.is_file():
            with orientation_log.open("rb") as src, log.open("ab") as dst:
                shutil.copyfileobj(src, dst)


_VIEW = re.compile(r"=== Processing image .*? \((\d+) / (\d+)\) ===")
_INLIERS = re.compile(r"(\d+) horizontal inliers, (\d+) vertical inliers")
_VECTOR = r"\s*([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s*$"
_VERTICAL = re.compile(r"\bVertical:" + _VECTOR)
_DOWN = re.compile(r"\bFound downward axis:" + _VECTOR)


def _supported_up(log: str, camera_up: list[float]) -> list[float] | None:
    # COLMAP 4.2 emits per-view estimates; an unknown log format fails closed.
    vectors: dict[int, np.ndarray] = {}
    down = None
    total = view = inliers = 0
    for line in log.splitlines():
        if match := _VIEW.search(line):
            view, total = map(int, match.groups())
            inliers = 0
        elif match := _INLIERS.search(line):
            inliers = int(match[2])
        elif 1 <= view <= total and inliers >= 10 and (match := _VERTICAL.search(line)):
            direction = _unit(match.groups())
            if direction is not None:
                vectors[view] = direction
        elif match := _DOWN.search(line):
            down = _unit(match.groups())
    hint = _unit(camera_up)
    if down is None or hint is None:
        return None
    agreement = float(down @ hint)
    if abs(agreement) < np.cos(np.deg2rad(30)):
        return None
    support = sum(abs(float(down @ v)) >= np.cos(np.deg2rad(15)) for v in vectors.values())
    if support < max(3, total * 0.30, len(vectors) * 0.75):
        return None
    return (down if agreement > 0 else -down).tolist()


def _unit(values) -> np.ndarray | None:
    try:
        vector = np.asarray(values, dtype=float)
    except (ValueError, TypeError):
        return None
    if vector.shape != (3,) or not np.isfinite(vector).all():
        return None
    length = np.linalg.norm(vector)
    return vector / length if np.isfinite(length) and length > 1e-8 else None
