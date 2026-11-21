#!/usr/bin/env python
"""按源相机位姿渲染 GLB，与对应去畸变照片并排对比。

读取 work/dense/sparse 的相机位姿，把 three.js 相机放到源相机位置/朝向
（COLMAP 相机：X 右、Y 下、Z 前 → three.js：X 右、Y 上、Z 后，即 D=diag(1,-1,-1)），
主点偏差用 setViewOffset 校正，垂直 FOV 由 fy 推出。输出 照片|模型 渲染并排图。

仅用于质量评估渲染，不改动线上服务。
"""
from __future__ import annotations

import argparse
import http.server
import json
import math
import shutil
import socketserver
import tempfile
import threading
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

import hole_diagnosis as hd

HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>html,body{margin:0;background:#111}canvas{display:block}</style>
<script type="importmap">
{"imports":{"three":"/lib/three.module.js","three/addons/":"/lib/addons/"}}
</script>
</head><body>
<script type="module">
import * as THREE from 'three';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';

const params = new URLSearchParams(location.search);
const W = parseInt(params.get('w') || '800'), H = parseInt(params.get('h') || '520');
const fx = parseFloat(params.get('fx')), fy = parseFloat(params.get('fy'));
const cx = parseFloat(params.get('cx')), cy = parseFloat(params.get('cy'));
const iw = parseFloat(params.get('iw')), ih = parseFloat(params.get('ih'));
const q = params.get('q').split(',').map(parseFloat);      // world-to-three-cam 四元数 (x,y,z,w)
const t = params.get('t').split(',').map(parseFloat);      // 相机中心（世界坐标）

const renderer = new THREE.WebGLRenderer({antialias:true, preserveDrawingBuffer:true});
renderer.setSize(W, H);
renderer.toneMapping = THREE.NoToneMapping;
document.body.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x202028);
const camera = new THREE.PerspectiveCamera(2*Math.atan(ih/(2*fy))*180/Math.PI, W/H, 0.01, 1e6);
camera.position.set(t[0], t[1], t[2]);
camera.quaternion.set(q[0], q[1], q[2], q[3]);
// 主点偏差：按比例映射到画布（画布本身视作全幅）
camera.setViewOffset(W, H, (cx - iw/2) * (W/iw), (cy - ih/2) * (H/ih), W, H);

scene.add(new THREE.HemisphereLight(0xffffff, 0x888888, 2.8));
const sun = new THREE.DirectionalLight(0xffffff, 1.8);
sun.position.set(t[0], t[1], t[2]);  // 机头灯：从相机位置照亮可见面
scene.add(sun);
scene.add(sun.target);

const loader = new GLTFLoader();
loader.load(params.get('model'), (gltf) => {
  scene.add(gltf.scene);
  window.MODEL_READY = true;
  window.DEBUG = (() => {
    const box = new THREE.Box3().setFromObject(gltf.scene);
    camera.updateMatrixWorld(true);
    const view = new THREE.Vector3(0, 0, 0).applyMatrix4(camera.matrixWorldInverse);
    return {
      box: [box.min.toArray(), box.max.toArray()],
      cameraPos: camera.position.toArray(),
      worldZeroInView: view.toArray(),
      camForward: new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion).toArray(),
    };
  })();
  renderer.render(scene, camera);
}, undefined, (err) => { window.LOAD_ERROR = String(err); });
window.RENDER = () => { renderer.render(scene, camera); return true; };
</script></body></html>
"""


def quaternion_from_matrix(R: "list[list[float]]") -> tuple[float, float, float, float]:
    """three.js 四元数 (x,y,z,w)，来自旋转矩阵。"""
    m00, m01, m02 = R[0]
    m10, m11, m12 = R[1]
    m20, m21, m22 = R[2]
    tr = m00 + m11 + m22
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return ((m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s, 0.25 * s)
    if m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2
        return (0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s)
    if m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2
        return ((m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s)
    s = math.sqrt(1.0 + m22 - m00 - m11) * 2
    return ((m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s)


def serve(root: Path, port: int):
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(root), **kw)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def quat_xyzw_to_matrix(q):
    """scipy 风格 (x, y, z, w) 四元数 → 旋转矩阵。"""
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w)
    x, y, z, w = x / n, y / n, z / n, w / n
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ]


def mat_mul(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def mat_vec(A, v):
    return [sum(A[i][k] * v[k] for k in range(3)) for i in range(3)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-dir", required=True)
    ap.add_argument("--glb", required=True)
    ap.add_argument("--photo", required=True, help="work/dense/images 里的文件名")
    ap.add_argument("--out", required=True, help="输出并排 PNG")
    ap.add_argument("--baked-quat", help="GLB 烘焙旋转 (x,y,z,w)，如 manifest 的 rotation_xyzw")
    ap.add_argument("--baked-center", help="烘焙旋转中心 (x,y,z)")
    ap.add_argument("--render-w", type=int, default=760)
    ap.add_argument("--render-h", type=int, default=500)
    ap.add_argument("--port", type=int, default=18222)
    args = ap.parse_args()

    job = Path(args.job_dir)
    sparse = job / "work" / "dense" / "sparse"
    cameras = hd.read_cameras_bin(sparse / "cameras.bin")
    records = {r["name"]: r for r in hd.read_images_bin(sparse / "images.bin")}
    rec = records[args.photo]
    cam = cameras[rec["camera_id"]]
    fx, fy, cx, cy = cam["params"][:4]
    iw, ih = cam["width"], cam["height"]
    R = hd.quat_to_R(rec["qvec"])          # world-to-cam
    D = [[1, 0, 0], [0, -1, 0], [0, 0, -1]]
    DR = [[sum(D[i][k] * R[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    # three.js 四元数是 相机局部→世界；目标 world→view 旋转是 D·R，故取其转置
    R3 = [[DR[j][i] for j in range(3)] for i in range(3)]
    # COLMAP: X_cam = R·X + t → 相机中心 C = -Rᵀ·t
    Rt = [[R[j][i] for j in range(3)] for i in range(3)]
    C = [-sum(Rt[i][k] * rec["tvec"][k] for k in range(3)) for i in range(3)]
    if args.baked_quat:
        # GLB 顶点已烘焙 X_b = R_b·(X−c)：把相机变换到烘焙坐标系
        Rb = quat_xyzw_to_matrix([float(v) for v in args.baked_quat.split(",")])
        c = [float(v) for v in args.baked_center.split(",")]
        R3 = mat_mul(Rb, R3)
        C = mat_vec(Rb, [C[i] - c[i] for i in range(3)])
    qx, qy, qz, qw = quaternion_from_matrix(R3)

    with tempfile.TemporaryDirectory(prefix="srcview-") as tmp:
        root = Path(tmp)
        (root / "lib").mkdir()
        three_root = Path(__file__).resolve().parents[1] / "frontend" / "node_modules" / "three"
        shutil.copy(three_root / "build" / "three.module.js", root / "lib" / "three.module.js")
        shutil.copytree(three_root / "examples" / "jsm", root / "lib" / "addons")
        shutil.copy(args.glb, root / "model.glb")
        (root / "index.html").write_text(HTML)
        qs = (f"w={args.render_w}&h={args.render_h}&fx={fx}&fy={fy}&cx={cx}&cy={cy}"
              f"&iw={iw}&ih={ih}&q={qx},{qy},{qz},{qw}&t={C[0]},{C[1]},{C[2]}&model=model.glb")
        httpd = serve(root, args.port)
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
            page = browser.new_page(viewport={"width": args.render_w * 2 + 12,
                                              "height": args.render_h + 8})
            page.goto(f"http://127.0.0.1:{args.port}/index.html?{qs}")
            page.wait_for_function("window.MODEL_READY === true || window.LOAD_ERROR", timeout=60000)
            if page.evaluate("window.LOAD_ERROR"):
                raise RuntimeError(page.evaluate("window.LOAD_ERROR"))
            print("DEBUG:", json.dumps(page.evaluate("window.DEBUG")))
            page.evaluate("window.RENDER()")
            page.wait_for_timeout(300)
            page.evaluate("window.RENDER()")
            shot = page.screenshot(path=str(Path(args.out).with_suffix(".raw.png")))
            browser.close()
        httpd.shutdown()

    raw = Image.open(Path(args.out).with_suffix(".raw.png")).convert("RGB")
    render = raw.crop((0, 0, args.render_w, raw.height))
    with Image.open(root_photo := job / "work" / "dense" / "images" / args.photo) as src:
        photo = src.convert("RGB")
    scale = render.height / photo.height
    photo = photo.resize((max(1, round(photo.width * scale)), render.height))
    sheet = Image.new("RGB", (render.width + photo.width + 8, render.height), (17, 17, 17))
    sheet.paste(render, (0, 0))
    sheet.paste(photo, (render.width + 8, 0))
    sheet.save(args.out)
    Path(args.out).with_suffix(".raw.png").unlink()
    print(f"saved {args.out}  camera={args.photo}  glb={Path(args.glb).name}")


if __name__ == "__main__":
    main()
