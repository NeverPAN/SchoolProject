#!/usr/bin/env python
"""阶段 8 可行性实验：区域级指标对比（局部视角看"更高分辨率重跑"的收益与边界）。

在指定源相机的视野中央取一个三维包围盒（代表"用户框选的区域"），对比两份
不同分辨率重建产物在该区域内/外的指标：
- 区域内点数、点密度（最近邻间距）、网格面密度；
- 区域外指标是否保持（对应"非目标区域不被破坏"）；
- 能看到该区域的相机列表（局部更新的调度依据）。

边界结论与工程路线见 docs/dev-summary/10-local-reconstruction-feasibility.md。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

import hole_diagnosis as hd


def camera_sees_bbox_center(center, R, t, K, w, h, zmin=1.0):
    cam = R @ center + t
    z = cam[2]
    if z < zmin:
        return False
    u = K[0, 0] * cam[0] / z + K[0, 2]
    v = K[1, 1] * cam[1] / z + K[1, 2]
    return 0 <= u < w and 0 <= v < h


def region_stats(points, lo, hi, sample=200000):
    mask = ((points >= lo) & (points <= hi)).all(axis=1)
    pts = points[mask]
    if len(pts) > sample:
        pts = pts[np.random.default_rng(0).choice(len(pts), sample, replace=False)]
    if len(pts) < 2:
        return {"points": int(mask.sum())}
    from scipy.spatial import cKDTree
    d, _ = cKDTree(pts).query(pts, k=2, workers=-1)
    return {
        "points": int(mask.sum()),
        "nn_spacing_median": float(np.median(d[:, 1])),
        "bbox_extent": [float(x) for x in (hi - lo)],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-a", required=True, help="低分辨率任务工作区（含 work/dense 与 checkpoints）")
    ap.add_argument("--cloud-a", required=True)
    ap.add_argument("--mesh-a", required=True)
    ap.add_argument("--job-b", required=True, help="高分辨率任务工作区")
    ap.add_argument("--cloud-b", required=True)
    ap.add_argument("--mesh-b", required=True)
    ap.add_argument("--camera", required=True, help="源相机图片名（框选视角）")
    ap.add_argument("--center-frac", type=float, default=0.4, help="视野中央比例")
    ap.add_argument("--work-scale", type=int, default=2000)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    def read_cam(job):
        dense = Path(job) / "work" / "dense"
        return hd.read_cameras_bin(dense / "sparse" / "cameras.bin"), \
            hd.read_images_bin(dense / "sparse" / "images.bin"), dense

    cams_a, imgs_a, dense_a = read_cam(args.job_a)
    rec = next(r for r in imgs_a if r["name"] == args.camera)
    cam = cams_a[rec["camera_id"]]
    fx, fy, cx, cy = cam["params"][:4]
    iw, ih = cam["width"], cam["height"]
    K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]])
    R = hd.quat_to_R(rec["qvec"])
    t = np.asarray(rec["tvec"])

    # 视野中央的像素框 → 反投影参考深度（取该相机看到的点云中位深度）
    cloud_a, _, _ = hd.read_ply(Path(args.cloud_a))
    cam_pts = cloud_a @ R.T + t
    z_cam = cam_pts[:, 2]
    vis = z_cam > 0.5
    uvx = fx * cam_pts[:, 0] / np.where(vis, z_cam, 1) + cx
    uvy = fy * cam_pts[:, 1] / np.where(vis, z_cam, 1) + cy
    cx0, cx1 = iw * (0.5 - args.center_frac / 2), iw * (0.5 + args.center_frac / 2)
    cy0, cy1 = ih * (0.5 - args.center_frac / 2), ih * (0.5 + args.center_frac / 2)
    central = vis & (uvx >= cx0) & (uvx <= cx1) & (uvy >= cy0) & (uvy <= cy1)
    z_med = float(np.median(z_cam[central])) if central.sum() > 100 else float(np.median(z_cam[vis]))

    # 中央像素框反投影到 z_med 平面 → 世界坐标包围盒（局部 "框选"）
    corners_px = [(cx0, cy0), (cx1, cy0), (cx1, cy1), (cx0, cy1)]
    dirs = []
    for px, py in corners_px:
        xc = (px - cx) / fx
        yc = (py - cy) / fy
        d_cam = np.array([xc, yc, 1.0])
        d_world = R.T @ (d_cam * z_med - t)
        dirs.append(d_world)
    origin = -R.T @ t
    # 用世界轴对齐盒（与查看器框选一致），取 5-95% 分位避免长尾
    sel = cloud_a[central]
    lo = np.quantile(sel, 0.05, axis=0)
    hi = np.quantile(sel, 0.95, axis=0)
    pad = (hi - lo) * 0.05 + 1e-6
    lo, hi = lo - pad, hi + pad

    cloud_b, _, _ = hd.read_ply(Path(args.cloud_b))
    mesh_a_v, _, mesh_a_f = hd.read_ply(Path(args.mesh_a))
    mesh_b_v, _, mesh_b_f = hd.read_ply(Path(args.mesh_b))

    result = {
        "camera": args.camera,
        "region_lo": [float(x) for x in lo], "region_hi": [float(x) for x in hi],
        "region_central_px": int(central.sum()),
        "cameras_seeing_region": [],
        "job_a": region_stats(cloud_a, lo, hi),
        "job_b": region_stats(cloud_b, lo, hi),
        "mesh_faces_in_region": {
            "job_a": int((((mesh_a_v[mesh_a_f].mean(axis=1) >= lo) & (mesh_a_v[mesh_a_f].mean(axis=1) <= hi)).all(axis=1)).sum()),
            "job_b": int((((mesh_b_v[mesh_b_f].mean(axis=1) >= lo) & (mesh_b_v[mesh_b_f].mean(axis=1) <= hi)).all(axis=1)).sum()),
        },
        "outside_points": {
            "job_a": int(((cloud_a < lo).any(axis=1) | (cloud_a > hi).any(axis=1)).sum()),
            "job_b": int(((cloud_b < lo).any(axis=1) | (cloud_b > hi).any(axis=1)).sum()),
        },
        "z_med": z_med,
    }
    # 能看到区域中心的相机
    center = (lo + hi) / 2
    for r in imgs_a:
        c = cams_a[r["camera_id"]]
        k = np.array([[c["params"][0], 0, c["params"][2]], [0, c["params"][1], c["params"][3]], [0, 0, 1]])
        if camera_sees_bbox_center(center, hd.quat_to_R(r["qvec"]), np.asarray(r["tvec"]), k, c["width"], c["height"]):
            result["cameras_seeing_region"].append(r["name"])

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "region_feasibility.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
