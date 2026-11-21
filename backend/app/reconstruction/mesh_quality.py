"""A2 通用网格质量诊断：面支撑、高度分带与顶部可观测性提示。

全部指标是"网格面到参与重建点云的最近距离"代理，不是几何真值；
二维投影或支撑率高不等于表面完整。高度分带的"上"方向来自稀疏
视角估计的 display_up；缺失时回退为最长包围盒轴并如实标注。
相机高度分析只提示顶部视角是否可能不足，不构成"顶部已还原"的证据。
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

import numpy as np


def _quat_to_R(q: tuple[float, float, float, float]) -> np.ndarray:
    w, x, y, z = q
    n = np.sqrt(w * w + x * x + y * y + z * z)
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def read_binary_ply_xyz(path: Path) -> np.ndarray:
    """通用二进制 PLY 顶点坐标读取（按 header 声明的偏移解析）。"""
    with path.open("rb") as f:
        header: list[bytes] = []
        while True:
            line = f.readline()
            if not line:
                raise ValueError(f"Incomplete PLY header: {path}")
            header.append(line)
            if line.strip() == b"end_header":
                break
        text = b"".join(header).decode("ascii", errors="replace")
        if "binary_little_endian" not in text:
            raise ValueError(f"仅支持 binary_little_endian PLY: {path}")
        count, props, in_vertex = 0, [], False
        for ln in text.splitlines():
            if ln.startswith("element"):
                in_vertex = ln.split()[1] == "vertex"
                if in_vertex:
                    count = int(ln.split()[2])
            elif ln.startswith("property") and in_vertex:
                ptype, name = ln.split()[1:3]
                props.append((name, ptype))
        if not count or not props:
            raise ValueError(f"PLY 缺少 vertex 属性: {path}")
        dtype = np.dtype([(n, {"float": "<f4", "double": "<f8", "uchar": "u1", "int": "<i4",
                               "uint": "<u4", "short": "<i2", "ushort": "<u2", "char": "i1"}[t])
                          for n, t in props])
        data = np.fromfile(f, dtype=dtype, count=count)
        if len(data) != count or f.read(1):
            raise ValueError(f"PLY 数据长度不匹配: {path}")
    return np.column_stack([data["x"], data["y"], data["z"]]).astype(float)


def read_camera_centers(images_bin: Path) -> np.ndarray:
    """从稀疏 images.bin 读取相机中心（world 坐标，-Rᵀt）。"""
    centers = []
    with images_bin.open("rb") as f:
        count, = struct.unpack("<Q", f.read(8))
        for _ in range(count):
            _, w, x, y, z, tx, ty, tz, _cam = struct.unpack("<i7di", f.read(64))
            name = bytearray()
            while (ch := f.read(1)) != b"\0":
                if not ch:
                    raise ValueError("Truncated image name")
                name.extend(ch)
            points, = struct.unpack("<Q", f.read(8))
            f.seek(points * struct.calcsize("<ddi"), 1)
            R = _quat_to_R((w, x, y, z))
            centers.append(-R.T @ np.array([tx, ty, tz]))
    if not centers:
        raise ValueError(f"No cameras in {images_bin}")
    return np.asarray(centers)


def support_metrics(mesh_path: Path, cloud_xyz: np.ndarray, *, up: list[float] | None = None,
                    max_face_samples: int = 200000) -> dict:
    """网格面对点云的最近距离支撑统计（面质心采样代理）。"""
    from scipy.spatial import cKDTree
    import trimesh

    mesh = trimesh.load(mesh_path, process=False, force="mesh")
    centroids = np.asarray(mesh.triangles_center)
    if len(centroids) > max_face_samples:
        rng = np.random.default_rng(0)
        centroids = centroids[rng.choice(len(centroids), max_face_samples, replace=False)]
    lo, hi = cloud_xyz.min(axis=0), cloud_xyz.max(axis=0)
    # 支撑半径与点云采样密度挂钩：3× 中位最近邻间距（最多抽样 5 万点估计）。
    sample_rng = np.random.default_rng(0)
    sample = cloud_xyz if len(cloud_xyz) <= 50000 else cloud_xyz[
        sample_rng.choice(len(cloud_xyz), 50000, replace=False)]
    spacing = float(np.median(cKDTree(cloud_xyz).query(sample, k=2)[0][:, 1]))
    radius = max(spacing * 3.0, 1e-9)
    distances = cKDTree(cloud_xyz).query(centroids)[0]

    axis_source = "display_up" if up and any(up) else "longest_axis_fallback"
    if up and any(up):
        axis = np.asarray(up, dtype=float)
        axis = axis / np.linalg.norm(axis)
    else:
        axis = np.eye(3)[int(np.argmax(hi - lo))]
    heights = centroids @ axis
    cloud_heights = cloud_xyz @ axis
    edges = np.quantile(cloud_heights, [1 / 3, 2 / 3])

    def band(mask_lo, mask_hi):
        sel = (heights >= mask_lo) & (heights <= mask_hi)
        if not sel.any():
            return None
        return float((distances[sel] <= radius).mean())

    supported = distances <= radius
    return {
        "proxy": "face-centroid nearest-distance to fused points; not geometric truth",
        "support_radius_model_units": radius,
        "radius_definition": "3x median nearest-neighbor spacing of the fused cloud (50k-point estimate)",
        "face_samples": int(len(centroids)),
        "mesh_faces_total": int(len(mesh.triangles_center)),
        "supported_face_fraction": float(supported.mean()),
        "unsupported_face_fraction": float((~supported).mean()),
        "height_axis_source": axis_source,
        "bands": {
            "bottom": band(-np.inf, edges[0]),
            "middle": band(edges[0], edges[1]),
            "top": band(edges[1], np.inf),
        },
        "height_edges_model_units": [float(edges[0]), float(edges[1])],
    }


def choose_mesh_candidate(conservative: dict, aggressive: dict) -> tuple[str, str]:
    """预声明门槛的候选选择：更激进候选必须支撑更高且外插增长 ≤ 3 个百分点。

    返回 (胜者名, 原因说明)。门槛固定，不因单次结果调整。
    """
    better_support = aggressive["supported_face_fraction"] > conservative["supported_face_fraction"]
    allowed_growth = aggressive["unsupported_face_fraction"] <= conservative["unsupported_face_fraction"] + 0.03
    if better_support and allowed_growth:
        return "aggressive", (
            f"低 trim 候选支撑率 {aggressive['supported_face_fraction']:.4f} > "
            f"{conservative['supported_face_fraction']:.4f}，无支撑比例增长 "
            f"{(aggressive['unsupported_face_fraction'] - conservative['unsupported_face_fraction']) * 100:.2f}pp ≤ 3pp"
        )
    return "conservative", (
        f"保留保守候选：支撑率 {conservative['supported_face_fraction']:.4f}，"
        f"无支撑 {conservative['unsupported_face_fraction']:.4f}；激进候选支撑更高={better_support}，"
        f"外插增长合格={allowed_growth}"
    )


def top_view_warning(camera_centers: np.ndarray, cloud_xyz: np.ndarray,
                     up: list[float] | None = None) -> str | None:
    """相机最高高度未进入点云上部 20% 高度带时提示顶部不可验证。"""
    axis = np.asarray(up, dtype=float) if up and any(up) else None
    if axis is None:
        return None
    axis = axis / np.linalg.norm(axis)
    lo = float((cloud_xyz @ axis).min())
    hi = float((cloud_xyz @ axis).max())
    band_start = hi - 0.2 * (hi - lo)
    camera_heights = camera_centers @ axis
    if float(camera_heights.max()) < band_start:
        return ("顶部视角不足：相机最高位置未进入点云上部 20% 高度带，"
                "顶部重建结果不可验证（未观测表面无法被保证还原）")
    return None
