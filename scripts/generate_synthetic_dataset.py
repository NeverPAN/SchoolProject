"""合成多视角测试数据集生成器（Smoke Test 用）。

纯 numpy/PIL 软件光栅化一个纹理化三维场景（两立方体 + 底面棋盘格墙），
从环绕视角渲染，输出带充足纹理特征的 JPEG 图片序列，可直接用于
COLMAP SfM/MVS 流程的端到端验证。不依赖任何外部数据下载。

用法:
  python generate_synthetic_dataset.py --out DIR --num 30 [--size 960]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

# 相机内参（与渲染一致）
FX = FY = 700.0
CX_OFFSET = 0.5
CY_OFFSET = 0.5


def make_textures(w: int, h: int, seed: int = 1) -> dict:
    """生成跨视角一致的高熵纹理：棋盘 + 中频模糊噪声 + 地标圆点。

    中频模糊噪声能抵抗透视重采样（跨视角一致），同时避免高频噪声的混叠问题；
    随机灰度棋盘提供大尺度结构。二者结合显著降低 SIFT ratio-test 的歧义性。
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    import cv2

    def blurred_noise(sigma: float, amplitude: float, base: float = 128.0) -> np.ndarray:
        n = rng.normal(base, amplitude, size=(h, w)).astype(np.float32)
        k = int(sigma * 6) | 1
        out = np.stack(
            [cv2.GaussianBlur(n, (k, k), sigma), cv2.GaussianBlur(rng.normal(base, amplitude, (h, w)).astype(np.float32), (k, k), sigma),
             cv2.GaussianBlur(rng.normal(base, amplitude, (h, w)).astype(np.float32), (k, k), sigma)],
            axis=-1,
        )
        return np.clip(out, 0, 255)

    tile = 64
    n_tw, n_th = w // tile, h // tile
    gray = rng.uniform(20, 245, size=(n_th + 2, n_tw + 2)).astype(np.float32)
    checker = gray[yy // tile, xx // tile]

    # 墙面：高对比棋盘(0.5) + 双倍频模糊噪声(0.5)
    noise_lo = blurred_noise(6.0, 70)
    noise_hi = blurred_noise(2.5, 45)
    tex_wall = np.clip(
        0.45 * checker[..., None] + 0.35 * noise_lo + 0.20 * noise_hi, 0, 255
    )

    # 物体1：彩色棋盘 + 噪声
    color_sq = rng.uniform(0, 255, size=(n_th + 2, n_tw + 2, 3)).astype(np.float32)
    cs = color_sq[yy // tile, xx // tile]
    obj1_noise = blurred_noise(4.0, 60)
    tex_obj1 = np.clip(0.55 * cs + 0.45 * obj1_noise, 0, 255)

    # 物体2：点阵 + 噪声
    obj2_noise = blurred_noise(4.0, 65)
    dots = (((xx % 96 - 48) ** 2 + (yy % 96 - 48) ** 2) < 500).astype(np.float32)
    tex_obj2 = np.clip(
        obj2_noise * 0.75 + dots[..., None] * np.array([235, 175, 60], np.float32) * 0.7, 0, 255
    )
    return {"wall": tex_wall, "obj1": tex_obj1, "obj2": tex_obj2}


def look_at(eye, center, up=np.array([0.0, 0.0, 1.0])):
    f = center - eye
    f = f / np.linalg.norm(f)
    r = np.cross(f, up)
    r = r / np.linalg.norm(r)
    u = np.cross(r, f)
    return np.stack([r, u, -f], axis=0)  # world->cam 旋转（相机看 -z）


def project(P, K, R, t, size):
    """P: (N,3) 世界坐标; R: world->cam; t: cam 位置. 返回像素坐标 + 深度。"""
    cam = (R @ P.T).T + t.reshape(1, 3)  # (N,3) 相机坐标，-z 朝前
    z = -cam[:, 2]  # 前向深度
    valid = z > 1e-6
    x = (K[0, 0] * cam[:, 0] / (-cam[:, 2] + 1e-12)) + K[0, 2]
    y = K[1, 2] - (K[1, 1] * cam[:, 1] / (-cam[:, 2] + 1e-12))  # 图像 y 向下，世界 z 向上
    return np.stack([x, y, z], axis=1), valid


def rasterize_scene(eye, size, textures, rng):
    """软件光栅化：墙 + 两个立方体。返回 RGB uint8 图像。"""
    W = H = size
    K = np.array([[FX * size / 960, 0, W * CX_OFFSET], [0, FY * size / 960, H * CY_OFFSET], [0, 0, 1.0]])
    center = np.array([0.0, 0.0, 0.3])
    R = look_at(np.asarray(eye), center)
    t = -R @ np.asarray(eye)

    # ---- 场景几何（世界坐标，z 向上）----
    quads = []  # (4 个 3D 角点, 纹理 key, UV 投影平面)
    # 房间三面墙 + 地面（盒子内表面）；UV 按所在平面取世界坐标，避免退化
    s = 2.2  # 半径
    quads.append((np.array([[-s, -s, 0], [s, -s, 0], [s, s, 0], [-s, s, 0]]), "wall", "xy"))  # 地面
    quads.append((np.array([[-s, s, 0], [s, s, 0], [s, s, 1.6], [-s, s, 1.6]]), "wall", "xz"))  # 背墙
    quads.append((np.array([[-s, -s, 0], [-s, s, 0], [-s, s, 1.6], [-s, -s, 1.6]]), "wall", "yz"))  # 左墙

    def box(c, hsz, tex):
        cx, cy, cz = c
        hx, hy, hz = hsz
        faces = [
            (np.array([[cx - hx, cy - hy, cz - hz], [cx + hx, cy - hy, cz - hz], [cx + hx, cy + hy, cz - hz], [cx - hx, cy + hy, cz - hz]]), tex, "xy"),
            (np.array([[cx - hx, cy - hy, cz + hz], [cx + hx, cy - hy, cz + hz], [cx + hx, cy + hy, cz + hz], [cx - hx, cy + hy, cz + hz]]), tex, "xy"),
            (np.array([[cx - hx, cy - hy, cz - hz], [cx - hx, cy + hy, cz - hz], [cx - hx, cy + hy, cz + hz], [cx - hx, cy - hy, cz + hz]]), tex, "yz"),
            (np.array([[cx + hx, cy - hy, cz - hz], [cx + hx, cy + hy, cz - hz], [cx + hx, cy + hy, cz + hz], [cx + hx, cy - hy, cz + hz]]), tex, "yz"),
            (np.array([[cx - hx, cy - hy, cz - hz], [cx + hx, cy - hy, cz - hz], [cx + hx, cy - hy, cz + hz], [cx - hx, cy - hy, cz + hz]]), tex, "xz"),
            (np.array([[cx - hx, cy + hy, cz - hz], [cx + hx, cy + hy, cz - hz], [cx + hx, cy + hy, cz + hz], [cx - hx, cy + hy, cz + hz]]), tex, "xz"),
        ]
        return faces

    quads += box((-0.35, -0.15, 0.42), (0.32, 0.30, 0.42), "obj1")
    quads += box((0.55, 0.30, 0.28), (0.24, 0.22, 0.28), "obj2")

    img = np.full((H, W, 3), 205.0, np.float32)
    zbuf = np.full((H, W), np.inf, np.float32)
    th, tw = 512, 512
    for corners, texkey, uvmode in quads:
        tex = textures[texkey]
        # 四边形重心插值光栅化（分两个三角形）
        for tri in ([0, 1, 2], [0, 2, 3]):
            p3 = corners[tri]
            pix, valid = project(p3, K, R, t, size)
            if not valid.all():
                continue
            xs, ys = pix[:, 0], pix[:, 1]
            # 背面剔除（屏幕 y 向下时，正面三角形的叉积为负）
            cross = (xs[1] - xs[0]) * (ys[2] - ys[0]) - (ys[1] - ys[0]) * (xs[2] - xs[0])
            if cross >= 0:
                continue
            x0, x1 = int(max(0, xs.min())), int(min(W - 1, xs.max()) + 1)
            y0, y1 = int(max(0, ys.min())), int(min(H - 1, ys.max()) + 1)
            if x1 <= x0 or y1 <= y0:
                continue
            gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)

            def edge(a, b, px, py):
                return (px - a[0]) * (b[1] - a[1]) - (py - a[1]) * (b[0] - a[0])

            area = edge(pix[0], pix[1], pix[2, 0], pix[2, 1])
            if abs(area) < 1e-9:
                continue
            w0 = edge(pix[1], pix[2], gx, gy) / area
            w1 = edge(pix[2], pix[0], gx, gy) / area
            w2 = edge(pix[0], pix[1], gx, gy) / area
            inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            if not inside.any():
                continue
            # 透视校正插值：屏幕空间线性插值 (P/d, 1/d)，再相除
            D0, D1, D2 = pix[0, 2], pix[1, 2], pix[2, 2]
            denom = w0 / D0 + w1 / D1 + w2 / D2
            depth = 1.0 / (denom + 1e-12)  # 插值后的视图深度
            patch_z = zbuf[y0:y1, x0:x1]
            upd = inside & (depth < patch_z)
            if not upd.any():
                continue
            world = (
                w0[:, :, None] * (p3[0] / D0)
                + w1[:, :, None] * (p3[1] / D1)
                + w2[:, :, None] * (p3[2] / D2)
            ) / denom[..., None]
            # UV：按面所在平面取两个世界坐标轴
            if uvmode == "xy":
                ua, va = world[..., 0], world[..., 1]
            elif uvmode == "xz":
                ua, va = world[..., 0], world[..., 2]
            else:
                ua, va = world[..., 1], world[..., 2]
            u = ((ua - ua.min()) / (np.ptp(ua) + 1e-9) * (tw - 1)).astype(int)
            v = ((va - va.min()) / (np.ptp(va) + 1e-9) * (th - 1)).astype(int)
            # 着色必须是世界坐标的确定函数（跨视角一致），否则描述子无法匹配
            shade = 0.82 + 0.18 * np.clip(world[..., 2] / 1.6, 0.0, 1.0)
            color = tex[np.clip(v, 0, th - 1), np.clip(u, 0, tw - 1)] * shade[..., None]
            target = img[y0:y1, x0:x1]
            target[upd] = color[upd]
            patch_z[upd] = depth[upd]

    img = np.clip(img, 0, 255).astype(np.uint8)
    # 轻微噪声，模拟真实相机
    img = img.astype(np.int16) + rng.normal(0, 1.0, img.shape).astype(np.int16)
    return np.clip(img, 0, 255).astype(np.uint8)


def generate(out_dir: Path, num: int, size: int = 960, seed: int = 7) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    textures = make_textures(512, 512, seed)
    meta = {"camera": {"fx": FX * size / 960, "fy": FY * size / 960, "cx": size * 0.5, "cy": size * 0.5,
                       "model": "SIMPLE_PINHOLE", "width": size, "height": size},
            "images": []}
    # 室内环绕轨道（相机在房间内，覆盖地面/两面墙/两个物体）
    for i in range(num):
        ang = 2 * np.pi * i / num
        radius = 1.65 + 0.1 * np.sin(2 * ang)
        elev = 0.30 + 0.22 * np.sin(ang * 2 + 1.0)  # 弧度，避免过低/过高
        eye = np.array([
            radius * np.cos(ang),
            radius * np.sin(ang),
            0.55 + radius * np.sin(elev) * 0.8,
        ])
        img = rasterize_scene(eye, size, textures, rng)
        name = f"img_{i:04d}.jpg"
        Image.fromarray(img).save(out_dir / name, quality=92)
        meta["images"].append({"name": name, "eye": eye.tolist(), "look_at": [0, 0, 0.4]})
    (out_dir / "ground_truth.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description="生成合成多视角测试数据集")
    ap.add_argument("--out", required=True)
    ap.add_argument("--num", type=int, default=30)
    ap.add_argument("--size", type=int, default=960)
    args = ap.parse_args()
    meta = generate(Path(args.out), args.num, args.size)
    print(f"已生成 {len(meta['images'])} 张 {args.size}x{args.size} 图片 -> {args.out}")


if __name__ == "__main__":
    main()
