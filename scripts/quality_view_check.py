#!/usr/bin/env python
"""对运行中服务的既有模型做浏览器实测：查看器渲染、交互、下载哈希。

用法（对第二阶段隔离服务）：
  backend/.venv/bin/python scripts/quality_view_check.py \
    --base-url http://127.0.0.1:18081 --username e2e_user \
    --project-id 2 --model-id 19 --model-file /path/to/model_photo.glb \
    --screenshots data/quality-check
不重建、不上传，只验收已发布的模型。
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys
from time import monotonic

from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase2_gui_test import SITE, STAMP  # noqa: E402  (复用夹具基址与常量)


def foreground(image: Image.Image) -> Image.Image:
    red, green, blue = image.split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue).point(lambda n: 255 if n < 170 else 0)


def frame(canvas, path: Path, condition, timeout=25, settle=True):
    """页面级截图 + 裁剪画布区域：大模型在软件渲染下元素稳定性检测不可用。"""
    box = canvas.bounding_box()
    page = canvas.page
    previous = None
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        shot = page.screenshot(timeout=15000)
        image = Image.open(BytesIO(shot)).convert("RGB").crop(
            (int(box["x"]), int(box["y"]),
             int(box["x"] + box["width"]), int(box["y"] + box["height"])))
        image.save(path)
        if condition(image):
            if not settle:
                return image
            if previous is not None and sum(ImageChops.difference(previous, image).convert("L").histogram()[2:]) <= 40:
                return image
            previous = image
        else:
            previous = None
    raise AssertionError(f"canvas did not settle: {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--project-id", type=int, required=True)
    ap.add_argument("--model-id", type=int, required=True)
    ap.add_argument("--model-file", type=Path, required=True, help="服务端登记的源文件，用于哈希比对")
    ap.add_argument("--expect-photo", action="store_true", help="断言查看器显示“照片贴图 · GLB”")
    ap.add_argument("--screenshots", type=Path, required=True)
    args = ap.parse_args()

    expected = sha256(args.model_file.read_bytes()).hexdigest()
    args.screenshots.mkdir(parents=True, exist_ok=True)
    results: dict = {"model_id": args.model_id, "expected_sha256": expected}

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
        page = browser.new_page(viewport={"width": 1366, "height": 900}, locale="en-US")
        errors: list[str] = []
        console: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
        base = args.base_url.rstrip("/")
        page.goto(base + "/login", wait_until="domcontentloaded")
        page.get_by_test_id("login-username").fill(args.username)
        page.get_by_test_id("login-password").fill(args.password)
        page.get_by_test_id("login-submit").click()
        page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
        page.goto(f"{base}/projects/{args.project_id}", wait_until="domcontentloaded")
        row = page.get_by_role("row").filter(has=page.get_by_role("cell", name=f"#{args.model_id}", exact=True))
        expect(row).to_be_visible(timeout=20000)
        row.get_by_role("button", name="查看", exact=True).click()
        expect(page.get_by_role("heading", name=f"三维模型查看 #{args.model_id}")).to_be_visible()
        if args.expect_photo:
            expect(page.get_by_text("照片贴图 · GLB", exact=True)).to_be_visible()
        canvas = page.get_by_label("三维模型画布")
        expect(canvas).to_be_visible()
        expect(page.get_by_role("button", name="重置视角")).to_be_enabled(timeout=30000)
        page.get_by_role("button", name="重置视角").click()

        home = frame(canvas, args.screenshots / "home.png", lambda i: foreground(i).getbbox() is not None)
        mask = foreground(home)
        area = mask.histogram()[255]
        assert area > home.width * home.height * 0.015, f"渲染前景过小: {area}px"
        results["foreground_px"] = area

        # 拖动旋转 + 重置
        box = canvas.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 120, y + 40, steps=10)
        page.mouse.up()
        orbit = frame(canvas, args.screenshots / "orbit.png",
                      lambda i: sum(ImageChops.difference(mask, foreground(i)).convert("L").histogram()[2:]) > area * 0.1,
                      settle=False)
        results["orbit_changed_px"] = sum(ImageChops.difference(mask, foreground(orbit)).convert("L").histogram()[2:])
        page.get_by_role("button", name="重置视角").click()

        def near_home(image: Image.Image) -> bool:
            fg = foreground(image)
            a = fg.histogram()[255]
            b1, b2 = mask.getbbox(), fg.getbbox()
            return (abs(a - area) <= area * 0.05 and b1 is not None and b2 is not None
                    and all(abs(p1 - p2) <= 8 for p1, p2 in zip(b1, b2)))

        frame(canvas, args.screenshots / "reset.png", near_home, timeout=45, settle=False)

        # 下载并比对哈希
        with page.expect_download() as dl:
            page.get_by_role("button", name="下载").click()
        download = dl.value
        target = args.screenshots / download.suggested_filename
        download.save_as(str(target))
        got = sha256(target.read_bytes()).hexdigest()
        results["download_sha256"] = got
        results["hash_match"] = got == expected
        assert got == expected, f"下载哈希不一致: {got} != {expected}"
        assert not errors, errors
        assert not console, console
        browser.close()

    results["status"] = "PASS"
    (args.screenshots / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
