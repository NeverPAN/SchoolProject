#!/usr/bin/env python
"""网格参数对照：在同一份融合点云上跑 Poisson depth×trim 组合，用同视角、深度感知的覆盖率评审。

判定方式（z-aware，不受背面/远处面干扰）：
- 每个相机把网格三角面中心投影到像平面；面中心是否"有点支撑"取决于
  最近稠密点在 (u/3px, v/3px, z/(2%·中位深度)) 缩放空间中的距离；
- 支撑面积 = 支撑面中心 splat 覆盖的像素；网格面积 = 视锥内面中心 splat；
  无支撑外插 = 两者之差；
- 稠密覆盖与 hole_diagnosis 同口径（点 splat 半径默认 2）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from scipy.spatial import cKDTree

from hole_diagnosis import project, quat_to_R, read_cameras_bin, read_images_bin, read_ply


def poisson(colmap: str, cloud: Path, out: Path, depth: int, trim: float,
            point_weight: float = 1.0) -> float:
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    args = [colmap, "poisson_mesher", "--input_path", str(cloud), "--output_path", str(out),
            "--PoissonMeshing.depth", str(depth), "--PoissonMeshing.trim", str(trim),
            "--PoissonMeshing.num_threads", "4"]
    if point_weight != 1.0:
        args += ["--PoissonMeshing.point_weight", str(point_weight)]
    subprocess.run(args, check=True, capture_output=True)
    return time.time() - t0


def splat_pixels(pts_px: np.ndarray, w: int, h: int, radius: int) -> np.ndarray:
    arr = np.zeros((h, w), dtype=np.uint8)
    u = pts_px[:, 0].astype(np.int64)
    v = pts_px[:, 1].astype(np.int64)
    ok = (u >= 0) & (u < w) & (v >= 0) & (v < h)
    arr[v[ok], u[ok]] = 255
    if radius >= 1:
        arr = np.asarray(Image.fromarray(arr, "L").filter(ImageFilter.MaxFilter(radius * 2 + 1)))
    return arr > 0


def sample_mesh_surface(verts, faces, K, R, t, w, h, px_per_sample):
    """按投影面积均匀采样三角面（重心采样），返回 (uv, z, 视锥内掩码)。

    大三角面中心稀疏会低估面积覆盖，因此按投影面积决定采样数，
    目标密度 px_per_sample 平方像素一个样本。
    """
    tri = verts[faces]  # F x 3 x 3
    cam = tri @ R.T + t
    z = cam[:, :, 2]
    front = (z > 1e-6).all(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        u = K[0, 0] * cam[:, :, 0] / z + K[0, 2]
        v = K[1, 1] * cam[:, :, 1] / z + K[1, 2]
    uv = np.stack([u, v], axis=2)
    area = 0.5 * np.abs((uv[:, 1, 0] - uv[:, 0, 0]) * (uv[:, 2, 1] - uv[:, 0, 1])
                        - (uv[:, 2, 0] - uv[:, 0, 0]) * (uv[:, 1, 1] - uv[:, 0, 1]))
    area = np.where(front, area, 0.0)
    k = np.clip(np.ceil(area / px_per_sample), 0, 200).astype(np.int64)
    idx = np.repeat(np.arange(len(faces)), k)
    if len(idx) == 0:
        return np.zeros((0, 2)), np.zeros(0), np.zeros(0, dtype=bool)
    r1 = np.sqrt(np.random.rand(len(idx)))
    r2 = np.random.rand(len(idx))
    a, b, c = tri[idx, 0], tri[idx, 1], tri[idx, 2]
    pts = a + r1[:, None] * (b - a) + r1[:, None] * r2[:, None] * (c - b - a + a)
    zs = (pts @ R.T + t)[:, 2]
    us = K[0, 0] * (pts @ R.T + t)[:, 0] / zs + K[0, 2]
    vs = K[1, 1] * (pts @ R.T + t)[:, 1] / zs + K[1, 2]
    ok = (zs > 1e-6) & (us >= 0) & (us < w) & (vs >= 0) & (vs < h)
    return np.stack([us, vs], axis=1), zs, ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--colmap", required=True)
    ap.add_argument("--cloud", required=True)
    ap.add_argument("--job-dir", required=True, help="含 work/dense 的任务工作区")
    ap.add_argument("--depths", default="8,9")
    ap.add_argument("--trims", default="10,8")
    ap.add_argument("--work-scale", type=int, default=2000)
    ap.add_argument("--max-cameras", type=int, default=24)
    ap.add_argument("--point-radius", type=float, default=2.0)
    ap.add_argument("--uv-tol-px", type=float, default=3.0)
    ap.add_argument("--z-tol-frac", type=float, default=0.02)
    ap.add_argument("--point-weights", default="1", help="逗号分隔，如 1,2,4")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    job = Path(args.job_dir)
    dense = job / "work" / "dense"
    cameras = read_cameras_bin(dense / "sparse" / "cameras.bin")
    images = read_images_bin(dense / "sparse" / "images.bin")
    if args.max_cameras and len(images) > args.max_cameras:
        step = len(images) / args.max_cameras
        images = [images[int(i * step)] for i in range(args.max_cameras)]

    full_w, full_h = Image.open(dense / "images" / images[0]["name"]).size
    scale = args.work_scale / max(full_w, full_h)
    w, h = max(1, round(full_w * scale)), max(1, round(full_h * scale))

    cloud, _, _ = read_ply(Path(args.cloud))
    np.random.seed(0)
    print(f"cloud {cloud.shape[0]} pts; cameras {len(images)}; work {w}x{h}")

    cams = []
    for rec in images:
        cam = cameras[rec["camera_id"]]
        fx, fy, cx, cy = cam["params"][:4]
        K = np.array([[fx * scale, 0, cx * scale], [0, fy * scale, cy * scale], [0, 0, 1]])
        R = quat_to_R(rec["qvec"])
        t = np.asarray(rec["tvec"])
        uv, ok = project(cloud, R, t, K)
        u, v = uv[:, 0], uv[:, 1]
        inside = ok & (u >= 0) & (u < w) & (v >= 0) & (v < h)
        z_cam = cloud @ R.T + t
        pts = np.stack([u[inside], v[inside], z_cam[inside, 2]], axis=1)
        z_med = float(np.median(pts[:, 2])) if len(pts) else 1.0
        dense_mask = splat_pixels(pts[:, :2], w, h, int(args.point_radius))
        cams.append({"K": K, "R": R, "t": t, "pts": pts, "z_med": z_med,
                     "dense_mask": dense_mask, "name": rec["name"]})
    dense_cov = [float(c["dense_mask"].mean()) for c in cams]
    print(f"dense coverage mean {np.mean(dense_cov):.4f} (splat 半径 {args.point_radius:g})")

    results = []
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    uv_tol = args.uv_tol_px
    z_tol = args.z_tol_frac
    for depth in (int(x) for x in args.depths.split(",")):
        for trim in (float(x) for x in args.trims.split(",")):
            for pw in (float(x) for x in args.point_weights.split(",")):
                tag = f"d{depth}-t{trim:g}-w{pw:g}"
                mesh_path = out_dir / f"mesh-{tag}.ply"
                elapsed = poisson(args.colmap, Path(args.cloud), mesh_path, depth, trim, pw)
                verts, _, faces = read_ply(mesh_path)
                supported_area, mesh_area = [], []
                pr = int(args.point_radius)
                px_per_sample = max(1.0, (pr * 0.9) ** 2)  # 采样密度略高于 splat 密度
                for c in cams:
                    uv, zs, ok = sample_mesh_surface(verts, faces, c["K"], c["R"], c["t"], w, h, px_per_sample)
                    if ok.sum() == 0:
                        supported_area.append(0.0)
                        mesh_area.append(0.0)
                        continue
                    uv_in, zs_in = uv[ok], zs[ok]
                    if len(c["pts"]):
                        tree = cKDTree(np.stack([
                            c["pts"][:, 0] / uv_tol,
                            c["pts"][:, 1] / uv_tol,
                            c["pts"][:, 2] / (z_tol * c["z_med"]),
                        ], axis=1))
                        q = np.stack([
                            uv_in[:, 0] / uv_tol,
                            uv_in[:, 1] / uv_tol,
                            zs_in / (z_tol * c["z_med"]),
                        ], axis=1)
                        dist, _ = tree.query(q, k=1, workers=-1)
                        sup = dist <= 1.0
                    else:
                        sup = np.zeros(len(uv_in), dtype=bool)
                    mesh_mask = splat_pixels(uv_in, w, h, pr)
                    sup_mask = splat_pixels(uv_in[sup], w, h, pr)
                    mesh_area.append(float(mesh_mask.mean()))
                    supported_area.append(float(sup_mask.mean()))
                tree_all = cKDTree(cloud)
                centers = verts[faces].mean(axis=1)
                dist_c, _ = tree_all.query(centers, k=1, workers=-1)
                row = {
                    "depth": depth, "trim": trim, "point_weight": pw, "seconds": round(elapsed, 2),
                    "faces": int(faces.shape[0]),
                    "mesh_area": float(np.mean(mesh_area)),
                    "supported_area": float(np.mean(supported_area)),
                    "dense_coverage": float(np.mean(dense_cov)),
                    "unsupported_area": float(np.mean(mesh_area)) - float(np.mean(supported_area)),
                    "p95_center_distance": float(np.quantile(dist_c, 0.95)),
                }
                results.append(row)
                print(json.dumps({k2: (round(v, 4) if isinstance(v, float) else v) for k2, v in row.items()},
                                 ensure_ascii=False))
                (out_dir / "sweep.json").write_text(
                    json.dumps({"cloud": str(args.cloud), "work_scale": args.work_scale,
                                "cameras": len(cams), "uv_tol_px": uv_tol, "z_tol_frac": z_tol,
                                "point_radius": args.point_radius, "results": results}, indent=2))
                del tree_all


if __name__ == "__main__":
    main()
