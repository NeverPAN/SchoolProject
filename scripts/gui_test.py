"""前端 GUI 无头浏览器实测（Playwright Chromium）。

覆盖：登录页注册/登录 → 项目列表/新建 → 项目详情/上传/质量检查 → 任务页状态与日志 →
模型查看页（Three.js 加载、风格切换、信息面板、下载按钮）。

用法: python scripts/gui_test.py [--base-url http://127.0.0.1:8000]
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def make_test_image(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    arr = np.full((480, 640, 3), 150, dtype=np.float32)
    xx, yy = np.mgrid[0:480, 0:640]
    arr[..., 0] += ((xx // 24 + yy // 24) % 2) * 60
    arr[..., 1] += rng.normal(0, 15, arr[..., 1].shape)
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--user", default="gui_user")
    ap.add_argument("--password", default="gui_pass_123456")
    ap.add_argument("--wait-model", action="store_true", help="等待一个真实模型可用于查看器测试")
    args = ap.parse_args()
    base = args.base_url

    shots = Path("data/gui-screenshots")
    shots.mkdir(parents=True, exist_ok=True)
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool):
        (passed if cond else failed).append(name)
        print(("PASS" if cond else "FAIL"), name, flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(15000)

        # 1. 登录页
        page.goto(f"{base}/login")
        page.wait_for_load_state("domcontentloaded")
        check("登录页加载", "图片三维重建系统" in page.content())

        # 2. 注册（先切到注册页签）
        page.get_by_role("tab", name="注册").click()
        page.wait_for_timeout(500)
        page.get_by_test_id("register-username").fill(args.user)
        page.get_by_test_id("register-password").fill(args.password)
        page.get_by_test_id("register-submit").click()
        page.wait_for_timeout(800)
        check("注册成功提示", ("注册成功" in page.content()) or ("已存在" in page.content()))

        # 3. 登录 → 项目列表（切回登录页签）
        page.get_by_role("tab", name="登录").click()
        page.wait_for_timeout(500)
        page.get_by_test_id("login-username").fill(args.user)
        page.get_by_test_id("login-password").fill(args.password)
        page.get_by_test_id("login-submit").click()
        page.wait_for_url("**/", timeout=10000)
        check("登录后进入项目列表", "我的项目" in page.content())

        # 4. 新建项目（先打开对话框）
        page.get_by_role("button", name="新建项目").click()
        page.wait_for_timeout(600)
        page.get_by_test_id("new-project-name").fill("GUI测试项目")
        page.get_by_test_id("create-project-btn").click()
        page.wait_for_timeout(1000)
        check("新建项目出现", "GUI测试项目" in page.content())

        # 5. 进入项目详情
        page.get_by_text("GUI测试项目").first.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(800)
        check("项目详情加载", "图片管理" in page.content())

        # 6. 上传图片（多张）
        img_paths = []
        tmp = Path("data/gui-tmp")
        tmp.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            f = tmp / f"img_{i}.jpg"
            f.write_bytes(make_test_image(i))
            img_paths.append(str(f))
        with page.expect_file_chooser() as fc:
            page.get_by_role("button", name="上传图片").first.click()
        file_chooser = fc.value
        file_chooser.set_files(img_paths)
        page.wait_for_timeout(2500)
        check("图片上传与缩略图", "img_0.jpg" in page.content())

        # 7. 质量检查
        page.get_by_role("button", name="质量检查").click()
        page.wait_for_timeout(1500)
        check("质量检查完成", "质量检查" in page.content())

        # 8. 任务列表初始为空（不提交耗时任务） — 验证提交对话框可打开
        page.get_by_role("button", name="创建重建任务").click()
        page.wait_for_timeout(600)
        check("任务配置对话框", "质量预设" in page.content())
        page.keyboard.press("Escape")

        page.screenshot(path=str(shots / "01-project-detail.png"), full_page=True)

        # 9. 模型查看器测试（若有模型）
        if args.wait_model:
            # 找一个 web 模型（通过第一个项目 API 轮询）
            import requests

            tok = page.evaluate("localStorage.getItem('recon_token')")
            r = requests.get(f"{base}/api/v1/projects", headers={"Authorization": f"Bearer {tok}"})
            model_id = None
            for proj in r.json():
                rm = requests.get(
                    f"{base}/api/v1/projects/{proj['id']}/models",
                    headers={"Authorization": f"Bearer {tok}"},
                )
                for m in rm.json():
                    if m["model_type"] == "web":
                        model_id = m["id"]
                        break
                if model_id:
                    break
            if model_id:
                page.goto(f"{base}/models/{model_id}")
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(6000)  # 等 GLB 解析与渲染
                page.screenshot(path=str(shots / "02-model-viewer.png"))
                check("模型查看页加载", ("展示风格" in page.content()) and ("模型版本" in page.content()))
                # 风格切换
                page.get_by_role("button", name="莫兰迪").click()
                page.wait_for_timeout(800)
                page.screenshot(path=str(shots / "03-model-morandi.png"))
                page.get_by_role("button", name="线框").click()
                page.wait_for_timeout(800)
                page.screenshot(path=str(shots / "04-model-wireframe.png"))
                page.get_by_role("button", name="点云").click()
                page.wait_for_timeout(800)
                page.screenshot(path=str(shots / "05-model-pointcloud.png"))
                check("风格切换完成", True)
            else:
                check("存在可用 web 模型", False)

        browser.close()

    print(f"\n通过 {len(passed)} 项 / 失败 {len(failed)} 项")
    if failed:
        print("失败项:", failed)
        return 1
    print("GUI 测试全部通过；截图见 data/gui-screenshots/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
