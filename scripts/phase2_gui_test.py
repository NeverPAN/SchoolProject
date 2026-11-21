"""Phase 2 Chromium regression. Requires Python Playwright, Chromium, and Pillow.

Fixture run (no service): python scripts/phase2_gui_test.py --dist frontend/dist
Live run (creates test data): python scripts/phase2_gui_test.py --base-url URL --dataset DIR
"""
from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
from io import BytesIO
import json
import mimetypes
from pathlib import Path
import struct
from urllib.parse import urlsplit
from uuid import uuid4
import os

from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright

STAMP = "2026-01-01T00:00:00Z"
SITE = "http://phase2-recon.test"
FALLBACK = "照片贴图失败，已回退到顶点颜色 GLB"


def textured_glb() -> bytes:
    image = Image.new("RGB", (2, 2), (235, 30, 140))
    png = BytesIO()
    image.save(png, "PNG")
    vertices = struct.pack("<9f", -1, -1, 0, 1, -1, 0, 0, 1, 0)
    uv = struct.pack("<6f", 0, 0, 1, 0, 0.5, 1)
    binary = vertices + uv + png.getvalue()
    gltf = {
        "asset": {"version": "2.0"}, "extensionsUsed": ["KHR_materials_unlit"], "scene": 0,
        "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "material": 0}]}],
        "materials": [{"doubleSided": True, "extensions": {"KHR_materials_unlit": {}},
                       "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "textures": [{"source": 0}], "images": [{"bufferView": 2, "mimeType": "image/png"}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [{"buffer": 0, "byteLength": 36},
                        {"buffer": 0, "byteOffset": 36, "byteLength": 24},
                        {"buffer": 0, "byteOffset": 60, "byteLength": len(png.getvalue())}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                       "min": [-1, -1, 0], "max": [1, 1, 0]},
                      {"bufferView": 1, "componentType": 5126, "count": 3, "type": "VEC2"}],
    }
    description = json.dumps(gltf).encode()
    description += b" " * (-len(description) % 4)
    binary += b"\0" * (-len(binary) % 4)
    chunks = struct.pack("<I4s", len(description), b"JSON") + description
    chunks += struct.pack("<I4s", len(binary), b"BIN\0") + binary
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks


class Fixtures:
    def __init__(self, dist: Path):
        self.dist = dist
        self.glb = textured_glb()
        self.project_created = False
        self.job_created = False
        self.uploads: list[list[str]] = []
        self.images: list[dict] = []
        self.unexpected: list[str] = []
        self.user = {"id": 1, "username": "phase2", "role": "user", "created_at": STAMP}
        self.model = {
            "id": 1, "project_id": 1, "job_id": 2, "model_type": "web", "format": "glb",
            "file_size": len(self.glb), "vertex_count": 3, "face_count": 1, "point_count": 0,
            "stage_origin": "PHOTO_TEXTURE", "created_at": STAMP, "display_up": None,
            "legacy_srgb_vertex_colors": False,
            "versions": [{"id": 1, "version_number": 1, "change_type": "initial", "created_at": STAMP}],
        }
        self.job = {
            "id": 1, "project_id": 1, "status": "SUCCEEDED", "current_stage": "EXPORTING",
            "progress": 100, "pipeline_mode": "colmap", "engine_version": "fixture",
            "config": {"preset": "standard", "matcher": "auto", "enable_dense": True, "enable_mesh": True},
            "created_at": STAMP, "started_at": STAMP, "finished_at": STAMP,
            "events": [{"id": i, "stage": stage, "status": status, "message": message,
                        "started_at": STAMP, "finished_at": STAMP, "duration_ms": 50}
                       for i, (stage, status, message) in enumerate([
                           ("MESHING", "completed", None), ("TEXTURING", "skipped", FALLBACK),
                           ("EXPORTING", "completed", None),
                       ], 1)],
        }

    def project(self) -> dict:
        return {"id": 1, "name": "Phase 2 regression", "description": "", "status": "active",
                "owner_id": 1, "created_at": STAMP, "updated_at": STAMP,
                "image_count": len(self.images), "total_image_bytes": sum(i["file_size"] for i in self.images),
                "job_count": int(self.job_created), "model_count": 1, "images": self.images,
                "quality_summary": {"pending": len(self.images)}}

    def upload(self, request) -> tuple[int, object]:
        headers = f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        parts = list(BytesParser(policy=policy.default).parsebytes(headers + request.post_data_buffer).iter_parts())
        files = [(part.get_filename(), part.get_payload(decode=True)) for part in parts
                 if part.get_param("name", header="content-disposition") == "files"]
        names = [name for name, _ in files]
        self.uploads.append(names)
        items, errors = [], []
        for name, data in files:
            if name.startswith("retry-") and len(names) > 1:
                errors.append({"filename": name, "code": "FIXTURE_ERROR", "error": "模拟上传失败"})
                continue
            item = {"id": len(self.images) + 1, "original_filename": name, "file_size": len(data),
                    "width": 64, "height": 64, "format": "jpg", "quality_status": "pending", "has_thumbnail": False}
            items.append(item)
            self.images.append(item)
        return 201, {"items": items, "partial_errors": errors} if errors else items

    def handle(self, route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        if not path.startswith("/api/"):
            file = self.dist / path.lstrip("/") if path.startswith("/assets/") else self.dist / "index.html"
            route.fulfill(path=file, content_type=mimetypes.guess_type(file)[0])
            return
        key = (request.method, path.removeprefix("/api/v1"))
        match key:
            case "POST", "/auth/login":
                status, body = 200, {"access_token": "fixture", "user": self.user}
            case "GET", "/projects":
                status, body = 200, [self.project()] if self.project_created else []
            case "POST", "/projects":
                self.project_created = True
                status, body = 201, self.project()
            case "GET", "/projects/1":
                status, body = 200, self.project()
            case "POST", "/projects/1/images":
                status, body = self.upload(request)
            case "GET", "/projects/1/jobs":
                status, body = 200, [self.job] if self.job_created else []
            case "POST", "/projects/1/jobs":
                self.job_created = True
                status, body = 202, self.job
            case "GET", "/jobs/1":
                status, body = 200, self.job
            case "GET", "/jobs/1/logs":
                route.fulfill(body=FALLBACK, content_type="text/plain; charset=utf-8")
                return
            case "GET", "/projects/1/models":
                status, body = 200, [self.model]
            case "GET", "/models/1":
                status, body = 200, self.model
            case "GET", "/models/1/file":
                route.fulfill(body=self.glb, content_type="model/gltf-binary")
                return
            case _:
                self.unexpected.append(str(key))
                status, body = 404, {"message": "Unexpected fixture request"}
        route.fulfill(status=status, json=body)


def inspect_canvas(canvas, path: Path) -> Image.Image:
    image = Image.open(BytesIO(canvas.screenshot(path=path, timeout=10000))).convert("RGB")
    matches = [(i % image.width, i // image.width) for i, (r, g, b) in enumerate(image.getdata())
               if r > 170 and g < 85 and 95 < b < 200]
    assert len(matches) > image.width * image.height * 0.01, f"Texture not visible in {path}"
    assert all(0 < x < image.width - 1 and 0 < y < image.height - 1 for x, y in matches), f"Model clipped in {path}"
    return image


def run_live(browser, args) -> None:
    extensions = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
    images = sorted(p.resolve() for p in args.dataset.iterdir() if p.is_file() and p.suffix.lower() in extensions)
    assert len(images) >= 3, "--dataset must contain at least 3 distinct reconstructable images"
    suffix = uuid4().hex[:8]
    password = os.environ.get("RECON_GUI_PASSWORD")
    if args.username:
        assert password, "Set RECON_GUI_PASSWORD for the specified --username"
    else:
        password = f"Phase2-{uuid4().hex}"
    username = args.username or f"phase2_{suffix}"
    project_name = f"Phase 2 GUI {suffix}"
    page = browser.new_page(viewport={"width": 1366, "height": 900}, accept_downloads=True)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(args.base_url.rstrip("/") + "/login", wait_until="domcontentloaded")
    if not args.username:
        page.get_by_role("tab", name="注册").click()
        page.get_by_test_id("register-username").fill(username)
        page.get_by_test_id("register-password").fill(password)
        page.get_by_test_id("register-submit").click()
        expect(page.get_by_role("tab", name="登录")).to_have_attribute("aria-selected", "true")
    page.get_by_test_id("login-username").fill(username)
    page.get_by_test_id("login-password").fill(password)
    page.get_by_test_id("login-submit").click()
    expect(page.get_by_text("我的项目", exact=True)).to_be_visible()
    page.get_by_role("button", name="新建项目").click()
    page.get_by_test_id("new-project-name").fill(project_name)
    page.get_by_test_id("create-project-btn").click()
    page.get_by_text(project_name, exact=True).click()
    expect(page.get_by_text("图片管理", exact=True)).to_be_visible()

    # Observe actual multipart requests without intercepting them.
    uploads = []
    page.on("request", lambda request: uploads.append(request) if request.method == "POST" and
            urlsplit(request.url).path.endswith("/images") else None)
    with page.expect_file_chooser() as choice:
        page.get_by_role("button", name="上传图片").click()
    choice.value.set_files(images)
    summary = page.get_by_test_id("upload-summary")
    expect(summary).to_contain_text(f"成功 {len(images)} 张 · 未成功 0 张", timeout=args.timeout * 1000)
    expect(summary.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "100")
    assert len(uploads) >= (len(images) + 11) // 12, "Upload did not split into bounded batches"
    page.screenshot(path=args.screenshots / "live-upload.png")

    page.get_by_role("button", name="创建重建任务").click()
    page.get_by_test_id("submit-job-btn").click()
    page.get_by_role("button", name="详情", exact=True).click()
    try:
        expect(page.get_by_text("重建完成，可在项目页的模型列表中查看与下载模型", exact=True)).to_be_visible(
            timeout=args.timeout * 1000,
        )
    finally:
        page.screenshot(path=args.screenshots / "live-task.png")
    if args.expect_texture != "any":
        expect(page.get_by_test_id("job-stage-timeline").get_by_text("照片贴图", exact=True)).to_be_visible()
    if args.expect_texture == "fallback":
        expect(page.get_by_role("alert").filter(has_text="未生成照片贴图")).to_be_visible()
    page.get_by_role("button", name="返回", exact=True).click()
    photo = page.get_by_role("row").filter(has=page.get_by_text("照片贴图", exact=True))
    if args.expect_texture == "photo":
        expect(photo.first).to_be_visible()
        target = photo.first
    elif args.expect_texture == "fallback":
        expect(photo).to_have_count(0)
        target = page.get_by_role("row").filter(has=page.get_by_role("button", name="查看", exact=True)).first
    else:
        target = page.get_by_role("row").filter(has=page.get_by_role("button", name="查看", exact=True)).first
    target.get_by_role("button", name="查看", exact=True).click()
    download_button = page.get_by_role("button", name="下载模型")
    if args.expect_texture == "photo":
        expect(page.get_by_text("照片贴图 · GLB", exact=True)).to_be_visible()
    if not args.skip_canvas:
        expect(page.get_by_role("button", name="重置视角")).to_be_enabled(timeout=args.timeout * 1000)
        expect(page.get_by_label("三维模型画布")).to_be_visible()
    page.screenshot(path=args.screenshots / "live-viewer.png")
    with page.expect_download() as info:
        download_button.click()
    download = info.value
    assert download.failure() is None
    with download.path().open("rb") as f:
        magic = f.read(4)
    assert magic == b"glTF" if args.expect_texture == "photo" else magic in (b"glTF", b"ply\n", b"ply\r")
    assert not errors, errors
    print(f"PASS live upload, task, viewer and download: {project_name}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Use a running service instead of API fixtures (creates test data)")
    parser.add_argument("--dataset", type=Path, help="Directory of reconstructable images for --base-url")
    parser.add_argument("--username", help="Existing live test account (password from RECON_GUI_PASSWORD); default registers a unique test account")
    parser.add_argument("--timeout", type=int, default=900, help="Live upload/task timeout in seconds")
    parser.add_argument("--expect-texture", choices=["any", "photo", "fallback"], default="any")
    parser.add_argument("--dist", type=Path, default=Path("frontend/dist"))
    parser.add_argument("--screenshots", type=Path, default=Path("data/gui-phase2"))
    parser.add_argument("--skip-canvas", action="store_true", help="Skip WebGL render assertions on unsupported hosts")
    args = parser.parse_args()
    if args.base_url and not (args.dataset and args.dataset.is_dir()):
        parser.error("--base-url requires a valid --dataset directory")
    if not args.base_url and not (args.dist / "index.html").is_file():
        parser.error("Build the frontend before running the fixture test")
    args.screenshots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
        if args.base_url:
            print("Environment preparation is complete; formal testing is beginning.", flush=True)
            run_live(browser, args)
            browser.close()
            return
        api = Fixtures(args.dist.resolve())
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(f"{SITE}/**", api.handle)
        print("Environment preparation is complete; formal testing is beginning.", flush=True)
        page.goto(SITE, wait_until="domcontentloaded")
        page.get_by_test_id("login-username").fill(api.user["username"])
        page.get_by_test_id("login-password").fill("fixture-password")
        page.get_by_test_id("login-submit").click()
        expect(page.get_by_text("我的项目", exact=True)).to_be_visible()
        page.get_by_role("button", name="新建项目").click()
        page.get_by_test_id("new-project-name").fill(api.project()["name"])
        page.get_by_test_id("create-project-btn").click()
        page.get_by_text(api.project()["name"], exact=True).click()
        expect(page.get_by_text("图片管理", exact=True)).to_be_visible()

        names = ["retry-one.jpg", "retry-two.jpg"] + [f"photo-{i:02d}.jpg" for i in range(13)]
        jpeg = BytesIO()
        Image.new("RGB", (64, 64), "gray").save(jpeg, "JPEG")
        with page.expect_file_chooser() as choice:
            page.get_by_role("button", name="上传图片").click()
        choice.value.set_files([{"name": name, "mimeType": "image/jpeg", "buffer": jpeg.getvalue()} for name in names])
        summary = page.get_by_test_id("upload-summary")
        expect(summary).to_contain_text("成功 13 张 · 未成功 2 张")
        expect(summary.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "100")
        assert api.uploads == [names[:12], names[12:]]
        expect(page.get_by_text("photo-12.jpg", exact=True)).to_be_visible()
        page.screenshot(path=args.screenshots / "01-partial-upload.png")
        page.get_by_role("button", name="重试 retry-one.jpg").click()
        expect(summary).to_contain_text("成功 1 张 · 未成功 0 张")
        expect(page.get_by_role("button", name="重试 retry-one.jpg")).to_have_count(0)
        expect(page.get_by_role("button", name="重试 retry-two.jpg")).to_be_enabled()
        assert api.uploads[-1] == ["retry-one.jpg"] and len(api.images) == 14
        print("PASS batch bounds, total progress, partial and full responses, isolated retry", flush=True)

        page.get_by_role("button", name="创建重建任务").click()
        page.get_by_test_id("submit-job-btn").click()
        page.get_by_role("button", name="详情", exact=True).click()
        expect(page.get_by_text("阶段时间线", exact=True)).to_be_visible()
        expect(page.get_by_test_id("job-stage-timeline").get_by_text("照片贴图", exact=True)).to_be_visible()
        expect(page.get_by_role("alert").filter(has_text="未生成照片贴图")).to_contain_text(FALLBACK)
        page.screenshot(path=args.screenshots / "02-texture-fallback.png")
        page.get_by_role("button", name="返回", exact=True).click()
        expect(page.get_by_text("照片贴图", exact=True)).to_be_visible()
        page.get_by_role("button", name="查看", exact=True).click()
        expect(page.get_by_text("照片贴图 · GLB", exact=True)).to_be_visible()
        print("PASS reconstruction workflow, texture fallback event and photo GLB markers", flush=True)

        if not args.skip_canvas:
            canvas = page.get_by_label("三维模型画布")
            reset = page.get_by_role("button", name="重置视角")
            expect(reset).to_be_enabled()
            before = inspect_canvas(canvas, args.screenshots / "03-canvas-desktop.png")
            page.get_by_role("button", name="侧转 90°").click()
            turned = inspect_canvas(canvas, args.screenshots / "04-canvas-rotated.png")
            assert ImageChops.difference(before, turned).getbbox(), "Orientation click did not change rendering"
            page.get_by_role("button", name="恢复朝向").click()
            page.get_by_role("button", name="线框").click()
            wireframe = Image.open(BytesIO(canvas.screenshot())).convert("RGB")
            assert not any(r > 170 and g < 85 and 95 < b < 200 for r, g, b in wireframe.getdata())
            page.get_by_role("button", name="写实").click()
            page.set_viewport_size({"width": 390, "height": 844})
            inspect_canvas(canvas, args.screenshots / "05-canvas-mobile.png")
            panel = page.get_by_text("展示风格", exact=True).bounding_box()
            bounds = canvas.bounding_box()
            assert bounds and panel and bounds["x"] + bounds["width"] <= 390
            assert panel["y"] >= bounds["y"] + bounds["height"], "Controls overlap the canvas"
            page.screenshot(path=args.screenshots / "06-viewer-mobile.png", full_page=True)
            print("PASS textured, unclipped desktop/mobile canvas, orientation and style controls", flush=True)
        else:
            print("SKIP canvas verification (--skip-canvas)", flush=True)
        assert not api.unexpected, api.unexpected
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    main()
