"""导出工具 — GLB 转换、点云着色网格、预览图。

- 网格 PLY → GLB：使用 trimesh；对无颜色网格，用稠密点云颜色做最近邻着色，
  便于 Web 端展示彩色模型；
- 预览 PNG：稠密点云降采样 + matplotlib 离屏渲染。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


def ply_stats(path: str | Path) -> dict:
    """读取 PLY 顶点/面数/点数统计。"""
    import trimesh

    m = trimesh.load(path, force="mesh", process=False)
    faces = len(m.faces) if hasattr(m, "faces") else 0
    vertices = len(m.vertices) if hasattr(m, "vertices") else 0
    return {
        "vertex_count": int(vertices),
        "face_count": int(faces),
        "point_count": int(vertices) if faces == 0 else 0,
    }


def colorize_mesh_from_cloud(mesh_ply: Path, cloud_ply: Path, out_ply: Path, max_samples: int = 400_000) -> bool:
    """用稠密点云颜色给无色网格顶点着色（scipy cKDTree 最近邻）。"""
    import trimesh
    from scipy.spatial import cKDTree

    mesh = trimesh.load(mesh_ply, force="mesh", process=False)
    if mesh.visual.kind == "vertex" and mesh.visual.vertex_colors is not None:
        return False  # 已有颜色
    cloud = trimesh.load(cloud_ply, force="mesh", process=False)
    if len(cloud.vertices) == 0:
        return False

    # 点云太大时降采样
    if len(cloud.vertices) > max_samples:
        idx = np.random.default_rng(42).choice(len(cloud.vertices), max_samples, replace=False)
        cloud_pts = cloud.vertices[idx]
        cloud_cols = cloud.visual.vertex_colors[idx, :3] if cloud.visual.kind == "vertex" else None
    else:
        cloud_pts = cloud.vertices
        cloud_cols = cloud.visual.vertex_colors[:, :3] if cloud.visual.kind == "vertex" else None

    if cloud_cols is None:
        return False
    tree = cKDTree(cloud_pts)
    dist, nn = tree.query(mesh.vertices, k=1)
    valid = dist < np.percentile(dist, 90) * 3  # 排除明显离群
    colors = np.zeros((len(mesh.vertices), 3), dtype=np.uint8)
    colors[valid] = cloud_cols[nn[valid]]
    mesh.visual = trimesh.visual.ColorVisuals(mesh, face_colors=None, vertex_colors=colors)
    mesh.export(out_ply)
    return True


def mesh_to_glb(mesh_ply: Path, out_glb: Path) -> dict:
    """PLY 网格 → GLB（含顶点颜色）。"""
    import trimesh

    mesh = trimesh.load(mesh_ply, force="mesh", process=True)
    out_glb.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(out_glb, file_type="glb")
    return ply_stats(out_glb)  # 统计实际意义不大，供参考


def pointcloud_to_glb(ply_path: Path, out_glb: Path, max_points: int = 3_000_000) -> bool:
    """稠密点云 PLY → GLB（POINTS 图元）。返回是否成功。

    trimesh 导出 PointCloud 为 glTF POINTS 基本图元；Three.js GLTFLoader 可加载。
    """
    try:
        import trimesh

        cloud = trimesh.load(ply_path, force="mesh", process=False)
        if len(cloud.faces) > 0 or len(cloud.vertices) == 0:
            return False
        pc = trimesh.PointCloud(cloud.vertices, colors=cloud.visual.vertex_colors if cloud.visual.kind == "vertex" else None)
        if len(pc.vertices) > max_points:
            idx = np.random.default_rng(42).choice(len(pc.vertices), max_points, replace=False)
            pc = trimesh.PointCloud(pc.vertices[idx], colors=pc.colors[idx] if pc.colors is not None else None)
        out_glb.parent.mkdir(parents=True, exist_ok=True)
        pc.export(out_glb, file_type="glb")
        return True
    except Exception:
        return False


def make_preview_png(ply_path: Path, out_png: Path, max_points: int = 60_000) -> bool:
    """稠密/稀疏点云 → 预览 PNG（matplotlib 离屏）。"""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import trimesh

        cloud = trimesh.load(ply_path, force="mesh", process=False)
        pts = cloud.vertices
        cols = cloud.visual.vertex_colors[:, :3] / 255.0 if cloud.visual.kind == "vertex" and cloud.visual.vertex_colors is not None else None
        if len(pts) == 0:
            return False
        if len(pts) > max_points:
            idx = np.random.default_rng(42).choice(len(pts), max_points, replace=False)
            pts = pts[idx]
            cols = cols[idx] if cols is not None else None

        fig = plt.figure(figsize=(8, 8), dpi=100)
        ax = fig.add_subplot(111, projection="3d")
        ax.scatter(pts[:, 0], pts[:, 1], pts[:, 2], s=0.5, c=cols, linewidths=0)
        ax.set_axis_off()
        ax.view_init(elev=25, azim=45)
        try:
            m2 = cloud.bounding_box
            ex = m2.extents.max()
            if ex > 0:
                c = cloud.bounds.mean(axis=0)
                ax.set_xlim(c[0] - ex / 2, c[0] + ex / 2)
                ax.set_ylim(c[1] - ex / 2, c[1] + ex / 2)
                ax.set_zlim(c[2] - ex / 2, c[2] + ex / 2)
        except Exception:
            pass
        out_png.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_png, bbox_inches="tight", pad_inches=0.05, facecolor="white")
        plt.close(fig)
        return True
    except Exception:
        return False
