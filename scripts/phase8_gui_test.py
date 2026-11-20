"""Chromium regression for the region extraction UI; no server or live data.

Build frontend/dist, then run: python scripts/phase8_gui_test.py
Requires Python Playwright, Chromium, and Pillow. API and GLB bytes are fixtures;
this verifies viewer geometry/UI, request sequencing, and download, not the worker.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
from math import isfinite
import mimetypes
from pathlib import Path
import struct
from urllib.parse import urlsplit

from PIL import Image
from playwright.sync_api import expect, sync_playwright

SITE = "http://phase8-recon.test"
ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-01-01T00:00:00Z"
PROJECT = "Phase 8 fixture"
EXTRACTION = "区域提取（非局部重建）"


def fixture_glb(depths: tuple[float, ...]) -> bytes:
    # Large coincident projections: center rays hit both planes, regardless of Z-axis rotation.
    # Nonzero absolute coordinates also catch omission of the inverse display transform.
    vertices = [(10 + x, 20 + y, z) for z in depths for x, y in
                [(-2, -2), (2, -2), (2, 2), (-2, -2), (2, 2), (-2, 2)]]
    binary = b"".join(struct.pack("<3f", *vertex) for vertex in vertices)
    document = {
        "asset": {"version": "2.0"}, "extensionsUsed": ["KHR_materials_unlit"], "scene": 0,
        "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "material": 0}]}],
        "materials": [{"doubleSided": True, "extensions": {"KHR_materials_unlit": {}},
                       "pbrMetallicRoughness": {"baseColorFactor": [0.75, 0.02, 0.4, 1]}}],
        "buffers": [{"byteLength": len(binary)}], "bufferViews": [{"buffer": 0, "byteLength": len(binary)}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(vertices), "type": "VEC3",
                       "min": [8, 18, min(depths)], "max": [12, 22, max(depths)]}],
    }
    description = json.dumps(document).encode()
    description += b" " * (-len(description) % 4)
    chunks = struct.pack("<I4s", len(description), b"JSON") + description
    chunks += struct.pack("<I4s", len(binary), b"BIN\0") + binary
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks


class Fixture:
    def __init__(self, dist: Path):
        self.dist = dist
        self.files = {1: fixture_glb((5.0, 7.0)), 2: fixture_glb((7.0,))}
        self.saved = []
        self.submitted = []
        self.polls = 0
        self.unexpected = []

    def model(self, model_id: int) -> dict:
        return {"id": model_id, "project_id": 1, "job_id": 9 if model_id == 2 else None,
                "model_type": "mesh", "format": "glb", "file_size": len(self.files[model_id]),
                "face_count": 2 if model_id == 2 else 4, "stage_origin": "LOCAL_REGION" if model_id == 2 else None,
                "created_at": STAMP, "versions": []}

    def job(self) -> dict:
        done = self.polls >= 2
        return {"id": 9, "project_id": 1, "status": "SUCCEEDED" if done else "QUEUED",
                "current_stage": None, "progress": 100 if done else 0, "pipeline_mode": "local_update",
                "config": {"region_id": 4, "target_model_id": 1}, "created_at": STAMP,
                "finished_at": STAMP if done else None, "events": []}

    def handle(self, route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        if not path.startswith("/api/"):
            file = self.dist / path.lstrip("/") if path.startswith("/assets/") else self.dist / "index.html"
            route.fulfill(path=file, content_type=mimetypes.guess_type(file)[0])
            return
        project = {"id": 1, "name": PROJECT, "description": "", "status": "active", "owner_id": 1,
                   "created_at": STAMP, "updated_at": STAMP, "image_count": 0, "total_image_bytes": 0,
                   "job_count": int(self.polls > 0), "model_count": 1 + int(self.polls >= 2), "images": []}
        key = (request.method, path.removeprefix("/api/v1"))
        status = 200
        match key:
            case "POST", "/auth/login":
                body = {"access_token": "fixture", "user": {"id": 1, "username": "phase8", "role": "user"}}
            case "GET", "/projects":
                body = [project]
            case "GET", "/projects/1":
                body = project
            case "GET", "/projects/1/jobs":
                body = [self.job()] if self.polls else []
            case "GET", "/projects/1/models":
                body = [self.model(1)] + ([self.model(2)] if self.polls >= 2 else [])
            case "GET", "/models/1" | "/models/2":
                body = self.model(int(path.rsplit("/", 1)[-1]))
            case "GET", "/models/1/file" | "/models/2/file" | "/models/1/download" | "/models/2/download":
                route.fulfill(body=self.files[int(path.split("/")[-2])], content_type="model/gltf-binary")
                return
            case "POST", "/models/1/regions":
                self.saved.append(request.post_data_json)
                status, body = 201, {"id": 4}
            case "POST", "/models/1/local-update":
                self.submitted.append(request.post_data_json)
                if len(self.submitted) == 1:
                    status, body = 503, {"code": "QUEUE_UNAVAILABLE", "message": "队列暂不可用"}
                else:
                    status, body = 202, self.job()
            case "GET", "/jobs/9":
                self.polls += 1
                body = self.job()
            case "GET", "/jobs/9/logs":
                route.fulfill(body="区域提取任务", content_type="text/plain; charset=utf-8")
                return
            case _:
                self.unexpected.append(key)
                status, body = 404, {"message": "Unexpected fixture request"}
        route.fulfill(status=status, json=body)


def canvas_colors(canvas, path: Path) -> tuple[int, int]:
    image = Image.open(BytesIO(canvas.screenshot(path=path, timeout=15000))).convert("RGB")
    orange = purple = 0
    pixels = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
    for r, g, b in pixels:
        orange += r > 230 and 60 < g < 170 and b < 65
        purple += r > 160 and g < 70 and b > 120
    return orange, purple


def check_download(page, button, data: bytes) -> None:
    with page.expect_download() as info:
        button.click()
    dl = info.value
    assert dl.failure() is None, dl.failure()
    assert sha256(dl.path().read_bytes()).digest() == sha256(data).digest(), dl.suggested_filename


def run(page, fixture: Fixture, output: Path) -> None:
    page.goto(SITE, wait_until="domcontentloaded")
    page.get_by_test_id("login-username").fill("phase8")
    page.get_by_test_id("login-password").fill("fixture-password")
    page.get_by_test_id("login-submit").click()
    page.get_by_text(PROJECT, exact=True).click()
    page.get_by_role("button", name="查看", exact=True).click()
    canvas = page.get_by_label("三维模型画布")
    enter = page.get_by_role("button", name="框选区域", exact=True)
    cancel = page.get_by_role("button", name="取消框选", exact=True)
    submit = page.get_by_role("button", name="创建区域提取任务", exact=True)
    expect(enter).to_be_enabled(timeout=15000)
    initial, purple = canvas_colors(canvas, output / "01-source.png")
    assert initial == 0 and purple > 1000, (initial, purple)
    page.get_by_role("button", name="点云", exact=True).click()
    expect(enter).to_be_disabled()
    page.get_by_role("button", name="写实", exact=True).click()
    enter.click()
    expect(page.get_by_text(EXTRACTION, exact=True)).to_be_visible()
    expect(submit).to_be_disabled()
    expect(canvas).to_have_css("cursor", "crosshair")

    def drag(start, end, *, release=True):
        bounds = canvas.bounding_box()
        assert bounds is not None
        pos = lambda point: (bounds["x"] + bounds["width"] * point[0], bounds["y"] + bounds["height"] * point[1])
        page.mouse.move(*pos(start))
        page.mouse.down()
        page.mouse.move(*pos(end), steps=5)
        if release:
            page.mouse.up()

    drag((0.5, 0.5), (0.502, 0.502))
    expect(page.get_by_role("alert").filter(has_text="选区太小")).to_be_visible()
    expect(submit).to_be_disabled()
    drag((0.04, 0.07), (0.14, 0.17))
    expect(page.get_by_role("alert").filter(has_text="框选未命中模型")).to_be_visible()
    expect(submit).to_be_disabled()
    drag((0.45, 0.45), (0.54, -0.03))  # release above canvas while pointer is captured
    expect(submit).to_be_enabled()
    orange, _ = canvas_colors(canvas, output / "02-outside-release.png")
    assert orange > 5, orange
    cancel.click()
    expect(enter).to_be_enabled()
    expect(canvas).not_to_have_css("cursor", "crosshair")
    assert canvas_colors(canvas, output / "03-cancel.png")[0] == 0
    assert not fixture.saved and not fixture.submitted

    page.get_by_role("button", name="侧转 90°").click()
    enter.click()
    drag((0.44, 0.45), (0.56, 0.55), release=False)
    page.keyboard.press("Escape")
    page.mouse.up()
    expect(enter).to_be_enabled()
    assert canvas_colors(canvas, output / "04-escape.png")[0] == 0
    enter.click()
    drag((0.44, 0.45), (0.56, 0.55))
    expect(submit).to_be_enabled()
    assert not fixture.saved and not fixture.submitted, "Drawing must not submit"
    assert canvas_colors(canvas, output / "05-rotated-selection.png")[0] > 5

    submit.click()
    expect(page.get_by_role("alert").filter(has_text="选区已保存，创建区域提取任务失败")).to_contain_text("队列暂不可用")
    assert len(fixture.saved) == 1 and fixture.submitted == [{"region_id": 4}]
    payload = fixture.saved[0]
    lo, hi = payload["bbox_min"], payload["bbox_max"]
    assert len(lo) == len(hi) == 3 and all(isfinite(a) and isfinite(b) and a < b for a, b in zip(lo, hi)), payload
    assert 8 < lo[0] < hi[0] < 12 and 18 < lo[1] < hi[1] < 22, payload
    assert 6.9 < lo[2] < 7 < hi[2] < 7.1, f"Rear plane or incorrect model coordinates included: {payload}"
    assert all(len(payload["camera"][key]) == 3 and all(map(isfinite, payload["camera"][key]))
               for key in ("position", "target")), payload
    page.screenshot(path=output / "06-queue-error.png")
    page.get_by_role("button", name="重试创建区域提取任务", exact=True).click()
    assert len(fixture.saved) == 1, "Retry must reuse the saved region"
    link = page.get_by_role("link", name="查看区域提取任务", exact=True)
    expect(link).to_be_visible()
    assert fixture.submitted == [{"region_id": 4}, {"region_id": 4}]
    page.screenshot(path=output / "07-created.png")

    link.click()
    expect(page.get_by_role("heading", name="区域提取任务 #9")).to_be_visible()
    expect(page.get_by_text(EXTRACTION, exact=True)).to_be_visible()
    expect(page.get_by_text("排队中", exact=True)).to_be_visible()
    page.screenshot(path=output / "08-queued.png")
    expect(page.get_by_text("区域提取完成，已导出独立模型", exact=True)).to_be_visible(timeout=10000)
    expect(page.get_by_text("COLMAP 真实重建", exact=True)).to_have_count(0)
    page.screenshot(path=output / "09-completed.png")
    page.get_by_role("link", name="前往项目查看与下载结果").click()
    expect(page.get_by_text(EXTRACTION, exact=True)).to_be_visible()
    result = page.get_by_role("row").filter(has_text="区域提取结果")
    expect(result).to_be_visible()
    check_download(page, result.get_by_role("button", name="下载", exact=True), fixture.files[2])
    source = page.get_by_role("row").filter(has=page.get_by_role("button", name="查看", exact=True)).filter(has_not_text="区域提取结果")
    check_download(page, source.get_by_role("button", name="下载", exact=True), fixture.files[1])
    result.get_by_role("button", name="查看", exact=True).click()
    expect(page.get_by_text("区域提取结果", exact=True)).to_be_visible()
    expect(enter).to_be_enabled(timeout=15000)
    assert canvas_colors(canvas, output / "10-result.png")[1] > 1000


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "frontend/dist")
    parser.add_argument("--screenshots", type=Path, default=ROOT / "data/gui-phase8")
    args = parser.parse_args()
    if not (args.dist / "index.html").is_file():
        parser.error("Build frontend/dist first")
    args.screenshots.mkdir(parents=True, exist_ok=True)
    fixture = Fixture(args.dist.resolve())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
        page = browser.new_page(viewport={"width": 1366, "height": 900}, accept_downloads=True)
        page_errors = []
        console_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.route(f"{SITE}/**", fixture.handle)
        print("Environment preparation is complete; formal testing is beginning.", flush=True)
        try:
            run(page, fixture, args.screenshots)
            assert not fixture.unexpected, fixture.unexpected
            assert not page_errors, page_errors
            # Chromium reports the deliberately injected queue failure as a failed resource.
            assert len(console_errors) == 1 and "503" in console_errors[0], console_errors
        except Exception:
            page.screenshot(path=args.screenshots / "failure.png")
            raise
        finally:
            browser.close()
    print("PASS region drag/miss/outside/cancel, rotated nearest-hit AABB, explicit submission and queue retry, labels, and download hashes")


if __name__ == "__main__":
    main()
