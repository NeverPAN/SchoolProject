#!/usr/bin/env python
"""同视角缺面诊断：把稠密点云和网格投影回源相机，量化覆盖率。

对任务的 work/dense 工作区逐相机计算：
- 照片工作分辨率（默认 1000px，与 MVS 实际输入一致）；
- fused-filtered 点云投影覆盖的像素比例；
- 原版 Poisson 网格 / 候选网格投影覆盖的像素比例；
- 网格像素附近（膨胀半径内）存在点云像素的比例（点支持率）。

输出 JSON 与若干相机的 [照片 | 点云 | 网格A | 网格B] 对比条带图。
仅用二进制模型解析 + numpy/PIL，不引入新依赖。
"""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

MODEL_PARAM_COUNTS = {
    0: 3,   # SIMPLE_PINHOLE fx cx cy
    1: 4,   # PINHOLE fx fy cx cy
    2: 4,   # SIMPLE_RADIAL
    3: 5,   # RADIAL
    4: 8,   # OPENCV
    5: 4,   # SIMPLE_RADIAL_FISHEYE
    6: 5,   # RADIAL_FISHEYE
    7: 8,   # OPENCV_FISHEYE
    8: 6,   # FULL_OPENCV
    9: 9,   # FOV
    10: 5,  # SIMPLE_RADIAL_TAN
}


def read_cameras_bin(path: Path) -> dict[int, dict]:
    out = {}
    with path.open("rb") as f:
        count, = struct.unpack("<Q", f.read(8))
        for _ in range(count):
            cam_id, model_id = struct.unpack("<ii", f.read(8))
            width, height = struct.unpack("<QQ", f.read(16))
            n = MODEL_PARAM_COUNTS.get(model_id)
            if n is None:
                raise ValueError(f"未知相机模型 id={model_id}")
            params = struct.unpack(f"<{n}d", f.read(8 * n))
            out[cam_id] = {"model": model_id, "width": width, "height": height, "params": params}
    return out


def read_images_bin(path: Path) -> list[dict]:
    out = []
    size = path.stat().st_size
    with path.open("rb") as f:
        count, = struct.unpack("<Q", f.read(8))
        for _ in range(count):
            _image_id, qw, qx, qy, qz, tx, ty, tz = struct.unpack("<i7d", f.read(60))
            cam_id, = struct.unpack("<i", f.read(4))
            name = b""
            while (ch := f.read(1)) not in (b"\0", b""):
                name += ch
            if not ch:
                raise ValueError("images.bin 名称流被截断")
            pts, = struct.unpack("<Q", f.read(8))
            if pts > (size - f.tell()) // 24:
                raise ValueError("images.bin 点数异常")
            f.seek(pts * 24, 1)
            out.append({"camera_id": cam_id, "qvec": (qw, qx, qy, qz),
                        "tvec": (tx, ty, tz), "name": name.decode()})
    return out


def read_ply(path: Path) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None]:
    """读 binary_little_endian PLY，返回 (顶点 float64 Nx3, 颜色或 None, 面或 None)。"""
    with path.open("rb") as f:
        header = []
        while True:
            line = f.readline().decode("ascii", "replace").strip()
            header.append(line)
            if line == "end_header":
                break
        text = "\n".join(header)
        if "binary_little_endian" not in text:
            raise ValueError(f"仅支持 binary_little_endian: {path}")
        props: dict[str, list[tuple[str, str]]] = {}
        current = None
        n_vertices = n_faces = 0
        for line in header:
            if line.startswith("element"):
                _, name, num = line.split()
                current = name
                props[current] = []
                if name == "vertex":
                    n_vertices = int(num)
                elif name == "face":
                    n_faces = int(num)
            elif line.startswith("property"):
                parts = line.split()
                if parts[1] == "list":
                    props[current].append((parts[2], parts[3], parts[4]))
                else:
                    props[current].append((parts[1], parts[2], "scalar"))
        fmt = {("float", "x"): "f4", ("double", "x"): "f8", ("uchar", "red"): "u1", ("uint8", "red"): "u1"}
        vprops = props["vertex"]
        dtype_fields = []
        for ptype, pname, _ in vprops:
            code = None
            for (t, base), c in fmt.items():
                if ptype == t and pname == base:
                    code = c
                    break
            if code is None:
                if ptype in ("float", "float32"):
                    code = "f4"
                elif ptype in ("double", "float64"):
                    code = "f8"
                else:
                    code = "u1"
            dtype_fields.append((pname, f"<{code}"))
        data = np.frombuffer(f.read(n_vertices * sum(np.dtype(c).itemsize for _, c in dtype_fields)),
                             dtype=np.dtype(dtype_fields), count=n_vertices)
        verts = np.stack([data["x"], data["y"], data["z"]], axis=1).astype(np.float64)
        colors = None
        if {"red", "green", "blue"} <= {p for _, p, _ in vprops}:
            colors = np.stack([data["red"], data["green"], data["blue"]], axis=1)
        faces = None
        if n_faces and "face" in props:
            fprops = props["face"][0]
            list_type, idx_type = fprops[0], fprops[1]  # (计数类型, 索引类型, 名称)
            it = {"uchar": "u1", "uint8": "u1", "int": "i4", "int32": "i4", "uint": "u4"}[idx_type]
            lt = {"uchar": "u1", "uint8": "u1"}[list_type]
            faces = np.empty((n_faces, 3), dtype=np.int64)
            raw = f.read()
            offset = 0
            for i in range(n_faces):
                cnt = raw[offset]
                offset += np.dtype(f"<{lt}").itemsize if lt == "u1" else 4
                if cnt != 3:
                    raise ValueError(f"面 {i} 有 {cnt} 个顶点，仅支持三角网格")
                faces[i] = np.frombuffer(raw, dtype=f"<{it}", count=3, offset=offset)
                offset += 12
        return verts, colors, faces


def quat_to_R(q: tuple[float, float, float, float]) -> np.ndarray:
    w, x, y, z = q
    n = np.sqrt(w * w + x * x + y * y + z * z)
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def project(points: np.ndarray, R: np.ndarray, t: np.ndarray, K: np.ndarray):
    cam = points @ R.T + t
    z = cam[:, 2]
    ok = z > 1e-6
    uv = np.empty((points.shape[0], 2))
    uv[:, 0] = K[0, 0] * cam[:, 0] / np.where(ok, z, 1) + K[0, 2]
    uv[:, 1] = K[1, 1] * cam[:, 1] / np.where(ok, z, 1) + K[1, 2]
    return uv, ok


def mask_from_points(uv, ok, w: int, h: int, radius: int) -> Image.Image:
    """单像素散点 + MaxFilter 膨胀 = 快速等价的圆点覆盖。"""
    u = uv[ok]
    inside = (u[:, 0] >= 0) & (u[:, 0] < w) & (u[:, 1] >= 0) & (u[:, 1] < h)
    u = u[inside]
    arr = np.zeros((h, w), dtype=np.uint8)
    arr[u[:, 1].astype(np.int64), u[:, 0].astype(np.int64)] = 255
    m = Image.fromarray(arr, "L")
    if radius >= 1:
        k = int(radius) * 2 + 1
        m = m.filter(ImageFilter.MaxFilter(k))
    return m


def coverage(mask: Image.Image) -> float:
    hist = mask.histogram()
    return hist[255] / max(1, mask.size[0] * mask.size[1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-dir", required=True, help="任务工作区（含 work/dense）")
    ap.add_argument("--cloud", required=True)
    ap.add_argument("--mesh-default", required=True)
    ap.add_argument("--mesh-candidate", required=True)
    ap.add_argument("--work-scale", type=int, default=1000, help="与 MVS 一致的工作分辨率长边")
    ap.add_argument("--point-radius", type=float, default=2.0)
    ap.add_argument("--support-dilate", type=int, default=7, help="点支持判定的膨胀核（奇数）")
    ap.add_argument("--strips", type=int, default=8)
    ap.add_argument("--max-cameras", type=int, default=0, help="0=全部；>0 时均匀抽样")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    job = Path(args.job_dir)
    dense = job / "work" / "dense"
    images_dir = dense / "images"
    cameras = read_cameras_bin(dense / "sparse" / "cameras.bin")
    images = read_images_bin(dense / "sparse" / "images.bin")

    if args.max_cameras and len(images) > args.max_cameras:
        step = len(images) / args.max_cameras
        images = [images[int(i * step)] for i in range(args.max_cameras)]
    one = images_dir / images[0]["name"]
    with Image.open(one) as im:
        full_w, full_h = im.size
    scale = args.work_scale / max(full_w, full_h)
    w, h = max(1, round(full_w * scale)), max(1, round(full_h * scale))

    cloud, _, _ = read_ply(Path(args.cloud))
    mesh_d, _, faces_d = read_ply(Path(args.mesh_default))
    mesh_c, _, faces_c = read_ply(Path(args.mesh_candidate))
    print(f"cloud {cloud.shape[0]} pts; mesh_default {faces_d.shape[0]} faces; "
          f"mesh_candidate {faces_c.shape[0]} faces; work {w}x{h}")

    results = []
    strip_pick = set(np.linspace(0, len(images) - 1, args.strips).round().astype(int))
    for idx, rec in enumerate(images):
        cam = cameras[rec["camera_id"]]
        fx, fy, cx, cy = cam["params"][:4]
        K = np.array([[fx * scale, 0, cx * scale], [0, fy * scale, cy * scale], [0, 0, 1]])
        R = quat_to_R(rec["qvec"])
        t = np.asarray(rec["tvec"])

        uv_p, ok_p = project(cloud, R, t, K)
        pm = mask_from_points(uv_p, ok_p, w, h, args.point_radius)

        entry = {"name": rec["name"]}
        for key, verts, faces in (("mesh_default", mesh_d, faces_d), ("mesh_candidate", mesh_c, faces_c)):
            uv_v, ok_v = project(verts, R, t, K)
            m = Image.new("L", (w, h), 0)
            d = ImageDraw.Draw(m)
            visible = faces[np.all(ok_v[faces], axis=1)]
            tri = uv_v[visible]
            for t3 in tri:
                d.polygon([tuple(t3[0]), tuple(t3[1]), tuple(t3[2])], fill=255)
            entry[key] = m
        sup_kernel = args.support_dilate + (args.support_dilate + 1) % 2
        pm_d = pm.filter(ImageFilter.MaxFilter(sup_kernel))
        pm_arr = np.asarray(pm_d) > 0
        entry["dense"] = pm
        res = {
            "name": rec["name"],
            "dense_coverage": coverage(pm),
            "mesh_default_coverage": coverage(entry["mesh_default"]),
            "mesh_candidate_coverage": coverage(entry["mesh_candidate"]),
        }
        for key in ("mesh_default", "mesh_candidate"):
            arr = np.asarray(entry[key]) > 0
            inter = arr & pm_arr
            res[f"{key}_point_support"] = float(inter.sum() / max(1, arr.sum()))
            res[f"{key}_beyond_points"] = float((arr & ~pm_arr).sum() / max(1, arr.sum()))
        results.append(res)

        if idx in strip_pick:
            photo = Image.open(images_dir / rec["name"]).convert("RGB").resize((w, h))
            photo = photo.point(lambda p: p // 3 + 40)
            panels = [photo]
            for key, color in (("dense", (0, 220, 0)), ("mesh_default", (255, 90, 0)),
                               ("mesh_candidate", (60, 120, 255))):
                base = photo.copy()
                arr = np.asarray(entry[key]) > 0
                px = np.asarray(base).copy()
                px[arr] = color
                panels.append(Image.fromarray(px))
            strip = Image.new("RGB", (w * len(panels) + 6 * (len(panels) - 1), h), (20, 20, 20))
            for i, p in enumerate(panels):
                strip.paste(p, (i * (w + 6), 0))
            out_strips = Path(args.out) / "strips"
            out_strips.mkdir(parents=True, exist_ok=True)
            strip.save(out_strips / f"{idx:03d}-{rec['name']}.png")

    def agg(key: str) -> dict:
        vals = np.array([r[key] for r in results], dtype=float)
        return {"mean": float(vals.mean()), "median": float(np.median(vals)),
                "p10": float(np.quantile(vals, 0.10)), "min": float(vals.min())}

    summary = {k: agg(k) for k in results[0] if k != "name"}
    report = {"work_scale": args.work_scale, "images": len(results), "point_radius": args.point_radius,
              "support_dilate": args.support_dilate, "cloud_points": int(cloud.shape[0]),
              "mesh_default_faces": int(faces_d.shape[0]), "mesh_candidate_faces": int(faces_c.shape[0]),
              "aggregate": summary, "per_camera": results}
    out_path = Path(args.out)
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / "diagnosis.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
