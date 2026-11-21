"""Standalone Chromium regression for Phase 2 formal ETH3D Bridge task #3.

Build frontend/dist first. Requires Python Playwright, Chromium, and Pillow.
Uses the built app and local file fixtures; no running service or reconstruction.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import struct
from time import monotonic
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageDraw, ImageOps
from playwright.sync_api import expect, sync_playwright

from phase2_gui_test import Fixtures, SITE, STAMP

VIEWPORT = {"width": 1366, "height": 900}
PROJECT = "Phase 2: ETH3D Bridge task #3"
FILES = {13: "model.glb", 14: "model_photo.glb"}
ORIGINAL_NAME = "DSC_1174.JPG"
ORIGINAL_SHA256 = "68efc6764138e8264f9f720462aa7db6523b19128e0c0b43fa087f5c1081fa99"


def inspect_glb(path: Path, photo: bool) -> dict:
    with path.open("rb") as stream:
        magic, version, total, size, kind = struct.unpack("<4sIII4s", stream.read(20))
        assert (magic, version, total, kind) == (b"glTF", 2, path.stat().st_size, b"JSON"), path
        gltf = json.loads(stream.read(size))
    # These formal files are flat scenes with no glTF node transforms. Fail closed
    # if a replacement export would need a scene graph traversal to derive bounds.
    nodes = gltf["scenes"][gltf.get("scene", 0)]["nodes"]
    positions = []
    faces = 0
    textured = 0
    for index in nodes:
        node = gltf["nodes"][index]
        assert not (node.keys() & {"matrix", "translation", "rotation", "scale", "children"}), node
        for prim in gltf["meshes"][node["mesh"]]["primitives"]:
            assert prim.get("mode", 4) == 4
            attrs = prim["attributes"]
            positions.append(gltf["accessors"][attrs["POSITION"]])
            count = gltf["accessors"][prim["indices"]]["count"]
            assert count % 3 == 0
            faces += count // 3
            material = gltf["materials"][prim["material"]]["pbrMetallicRoughness"]
            if "baseColorTexture" in material:
                assert photo and "TEXCOORD_0" in attrs
                texture = gltf["textures"][material["baseColorTexture"]["index"]]
                assert "bufferView" in gltf["images"][texture["source"]]
                textured += count // 3
            elif not photo:
                assert "COLOR_0" in attrs
    assert (textured > 0) == photo, path
    return {
        "file": path.name, "sha256": sha256(path.read_bytes()).hexdigest(),
        "vertices": sum(a["count"] for a in positions), "faces": faces, "textured_faces": textured,
        "min": [min(a["min"][i] for a in positions) for i in range(3)],
        "max": [max(a["max"][i] for a in positions) for i in range(3)],
    }


class MatchedFixtures(Fixtures):
    def __init__(self, dist: Path, job_dir: Path, assets: dict):
        super().__init__(dist)
        self.job_dir = job_dir
        self.project_created = self.job_created = True
        self.job = {**self.job, "id": 3, "events": []}
        self.served = []
        self.models = {
            model_id: {**self.model, "id": model_id, "job_id": 3, "created_at": STAMP,
                       "stage_origin": "PHOTO_TEXTURE" if model_id == 14 else "EXPORTING",
                       "file_size": (job_dir / name).stat().st_size,
                       "vertex_count": assets[model_id]["vertices"], "face_count": assets[model_id]["faces"],
                       # Neutralize the shared estimated-up hint to compare native orientation.
                       "display_up": None, "legacy_srgb_vertex_colors": False}
            for model_id, name in FILES.items()
        }

    def project(self) -> dict:
        return {**super().project(), "name": PROJECT, "model_count": len(self.models)}

    def handle(self, route) -> None:
        path = urlsplit(route.request.url).path
        if route.request.method == "GET":
            if path == "/api/v1/projects/1/models":
                route.fulfill(json=list(self.models.values()))
                return
            for model_id, name in FILES.items():
                endpoint = f"/api/v1/models/{model_id}"
                if path == endpoint:
                    route.fulfill(json=self.models[model_id])
                    return
                if path == f"{endpoint}/file":
                    route.fulfill(path=self.job_dir / name, content_type="model/gltf-binary")
                    self.served.append(name)
                    return
        super().handle(route)


def max_channel(image: Image.Image) -> Image.Image:
    red, green, blue = image.split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue)


def changed(a: Image.Image, b: Image.Image, threshold: int = 12) -> int:
    return sum(max_channel(ImageChops.difference(a, b)).histogram()[threshold + 1:])


def foreground(image: Image.Image) -> Image.Image:
    # The default gradient and grid are light. This conservative dark-pixel proxy
    # excludes them; it is not an exact segmentation of bright parts of the mesh.
    return max_channel(image).point(lambda n: 255 if n < 170 else 0)


def area(mask: Image.Image) -> int:
    return mask.histogram()[255]


def frame(canvas, path: Path, condition=lambda image: True) -> Image.Image:
    """Wait for the expected visual change and for OrbitControls damping to settle."""
    previous = None
    deadline = monotonic() + 25
    while monotonic() < deadline:
        image = Image.open(BytesIO(canvas.screenshot(timeout=10000))).convert("RGB")
        image.save(path)
        if condition(image):
            if previous is not None and changed(previous, image, threshold=2) <= 40:
                return image
            previous = image
        else:
            previous = None
    raise AssertionError(f"Expected canvas state did not settle; last frame: {path}")


def verify_visible(image: Image.Image) -> Image.Image:
    mask = foreground(image)
    bounds = mask.getbbox()
    assert bounds and area(mask) > image.width * image.height * 0.015, "Missing rendered model"
    left, top, right, bottom = bounds
    assert min(left, top, image.width - right, image.height - bottom) >= 4, "Model reaches canvas edge"
    return mask


def comparison_sheet(output: Path, vertex: Image.Image, photo: Image.Image, original: Path | None) -> None:
    panels = [("Vertex colors | model.glb", vertex), ("Photo texture | model_photo.glb", photo)]
    if original:
        with Image.open(original) as source:
            reference = ImageOps.exif_transpose(source).convert("RGB")
        reference.thumbnail((vertex.width, vertex.height), Image.Resampling.LANCZOS)
        panels.append((f"ETH3D input | {ORIGINAL_NAME} (not pose-matched)", reference))
    sheet = Image.new("RGB", (vertex.width * len(panels), vertex.height + 32), "#182030")
    labels = ImageDraw.Draw(sheet)
    for index, (label, image) in enumerate(panels):
        x = index * vertex.width
        sheet.paste(image, (x + (vertex.width - image.width) // 2, 32 + (vertex.height - image.height) // 2))
        labels.text((x + 8, 9), label, fill="white")
    sheet.save(output / "matched-view.png")


def run(browser, args, assets: dict) -> dict:
    api = MatchedFixtures(args.dist, args.job_dir, assets)
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1, locale="en-US")
    errors = []
    console_errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.route(f"{SITE}/**", api.handle)
    print("Environment preparation is complete; formal testing is beginning.", flush=True)
    page.goto(SITE, wait_until="domcontentloaded")
    page.get_by_test_id("login-username").fill(api.user["username"])
    page.get_by_test_id("login-password").fill("fixture-password")
    page.get_by_test_id("login-submit").click()
    page.get_by_text(PROJECT, exact=True).click()
    canvas = page.get_by_label("三维模型画布")
    reset = page.get_by_role("button", name="重置视角")
    images, masks = {}, {}
    for model_id, tag in [(13, "vertex"), (14, "photo")]:
        row = page.get_by_role("row").filter(has=page.get_by_role("cell", name=f"#{model_id}", exact=True))
        row.get_by_role("button", name="查看", exact=True).click()
        expect(page.get_by_role("heading", name=f"三维模型查看 #{model_id}")).to_be_visible()
        expect(page.get_by_text("照片贴图 · GLB", exact=True)).to_have_count(int(model_id == 14))
        expect(reset).to_be_enabled(timeout=30000)
        expect(canvas).to_be_visible()
        expect(page.get_by_role("alert")).to_have_count(0)
        reset.click()
        images[tag] = frame(canvas, args.output / f"{tag}-home.png", lambda image: area(foreground(image)) > 1000)
        verify_visible(images[tag])
        page.get_by_role("button", name="单色", exact=True).click()
        neutral = frame(canvas, args.output / f"{tag}-monochrome.png", lambda image: changed(images[tag], image) > 1000)
        masks[tag] = verify_visible(neutral)
        if model_id == 13:
            page.get_by_role("button", name="返回", exact=True).click()
        else:
            page.get_by_role("button", name="写实", exact=True).click()
            frame(canvas, args.output / "photo-restored-style.png", lambda image: changed(images[tag], image) <= 100)

    vertex, photo = images["vertex"], images["photo"]
    assert vertex.size == photo.size
    overlap = area(ImageChops.darker(masks["vertex"], masks["photo"])) / area(ImageChops.lighter(masks["vertex"], masks["photo"]))
    assert overlap >= 0.97, f"Monochrome framing differs (foreground IoU {overlap:.3f})"
    difference = changed(vertex, photo)
    assert difference >= photo.width * photo.height * 0.015, "Default materials look indistinguishable"
    comparison_sheet(args.output, vertex, photo, args.original)

    box = canvas.bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 110, y + 45, steps=12)
    page.mouse.up()
    home_mask = foreground(photo)
    home_area = area(home_mask)
    orbited = frame(canvas, args.output / "photo-orbit.png",
                    lambda image: area(ImageChops.difference(home_mask, foreground(image))) > home_area * 0.20)
    orbit_delta = area(ImageChops.difference(home_mask, verify_visible(orbited)))
    reset.click()
    orbit_reset = frame(canvas, args.output / "photo-orbit-reset.png", lambda image: changed(photo, image) <= 100)

    page.mouse.move(x, y)
    page.mouse.wheel(0, -420)
    zoom = frame(canvas, args.output / "photo-zoom.png", lambda image: area(foreground(image)) >= home_area * 1.10)
    zoom_ratio = area(verify_visible(zoom)) / home_area
    reset.click()
    restored = frame(canvas, args.output / "photo-zoom-reset.png", lambda image: changed(photo, image) <= 100)
    assert api.served == list(FILES.values()), api.served
    assert not (errors or console_errors or api.unexpected), (errors, console_errors, api.unexpected)
    return {"browser": browser.version, "viewport": VIEWPORT, "canvas": photo.size,
            "monochrome_foreground_iou": overlap, "material_pixels_changed": difference,
            "orbit_foreground_pixels_changed": orbit_delta, "photo_home_foreground_pixels": home_area,
            "orbit_reset_pixels_changed": changed(photo, orbit_reset),
            "zoom_foreground_area_ratio": zoom_ratio, "zoom_reset_pixels_changed": changed(photo, restored),
            "page_errors": errors, "console_errors": console_errors, "unexpected_fixture_requests": api.unexpected}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", type=Path, required=True, help="Directory containing task #3 GLBs")
    parser.add_argument("--original", type=Path, help=f"Optional byte-identical ETH3D {ORIGINAL_NAME}")
    parser.add_argument("--dist", type=Path, default=Path("frontend/dist"))
    parser.add_argument("--output", type=Path, default=Path("data/phase2-matched-view"))
    args = parser.parse_args()
    if not (args.dist / "index.html").is_file():
        parser.error("Build frontend/dist first or supply --dist")
    if args.original and sha256(args.original.read_bytes()).hexdigest() != ORIGINAL_SHA256:
        parser.error(f"--original must match the formal ETH3D {ORIGINAL_NAME} checksum")
    assets = {model_id: inspect_glb(args.job_dir / name, photo=model_id == 14) for model_id, name in FILES.items()}
    assert assets[13]["faces"] == assets[14]["faces"] == 27041, assets
    for key in ("min", "max"):
        assert assets[13][key] == assets[14][key], "Native GLB bounds differ; viewer auto-fit would not match"
    args.output.mkdir(parents=True, exist_ok=True)
    # Avoid retaining a previous PASS result if this run fails.
    results = args.output / "results.json"
    results.unlink(missing_ok=True)
    with sync_playwright() as p:
        with p.chromium.launch(args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"]) as browser:
            metrics = run(browser, args, assets)
    summary = {"status": "PASS", "task_id": 3, "assets": assets,
               "original": {"file": ORIGINAL_NAME, "sha256": ORIGINAL_SHA256, "pose_matched": False} if args.original else None,
               **metrics}
    results.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
