"""阶段 B：GLB 纹理资产校验与原色贴图可用性。"""
from __future__ import annotations

import json
import struct
from pathlib import Path


def _glb(tmp_path: Path, doc: dict, *, version: int = 2) -> Path:
    payload = json.dumps(doc).encode()
    padding = (4 - len(payload) % 4) % 4
    payload += b" " * padding
    data = struct.pack("<III", 0x46546C67, version, 12 + 8 + len(payload)) + \
        struct.pack("<II", len(payload), 0x4E4F534A) + payload
    path = tmp_path / "asset.glb"
    path.write_bytes(data)
    return path


def test_inspect_glb_reports_embedded_textured_assets(tmp_path):
    from app.reconstruction.glb_assets import inspect_glb

    path = _glb(tmp_path, {
        "asset": {"version": "2.0"},
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}},
                                   {"attributes": {"POSITION": 0}}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}},
                      {"doubleSided": True}],
        "textures": [{"source": 0}],
        "images": [{"bufferView": 0, "mimeType": "image/png"}],
    })
    assets = inspect_glb(path)
    assert assets == {
        "primitives_total": 2, "primitives_with_uv": 1, "materials_total": 2,
        "materials_with_base_color_texture": 1, "texture_count": 1,
        "embedded_textures": True, "external_texture_uris": [], "double_sided_materials": 1,
    }


def test_inspect_glb_flags_external_uri_as_not_embedded(tmp_path):
    from app.reconstruction.glb_assets import inspect_glb

    path = _glb(tmp_path, {
        "asset": {"version": "2.0"},
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "textures": [{"source": 0}],
        "images": [{"uri": "atlas.png"}],
    })
    assets = inspect_glb(path)
    assert assets["embedded_textures"] is False
    assert assets["external_texture_uris"] == ["atlas.png"]


def test_inspect_glb_rejects_invalid_or_legacy_files(tmp_path):
    from app.reconstruction.glb_assets import inspect_glb

    legacy = _glb(tmp_path, {"asset": {"version": "1.0"}}, version=1)
    assert inspect_glb(legacy) is None
    assert inspect_glb(tmp_path / "missing.glb") is None
    junk = tmp_path / "junk.glb"
    junk.write_bytes(b"not a glb file at all")
    assert inspect_glb(junk) is None


def test_model_detail_exposes_texture_assets_from_real_artifact(client, auth_headers, project, upload_images):
    """mock 网格 GLB 是真实 trimesh 导出（无纹理）：如实报告零纹理。"""
    import time

    from tests.test_jobs import wait_job

    upload_images(4)
    job = client.post(f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"},
                      headers=auth_headers).json()
    detail = wait_job(client, auth_headers, job["id"])
    time.sleep(0.3)  # 模型发布与任务成功在同一事务，等待最终读取可见
    assert detail["status"] == "SUCCEEDED"
    models = client.get(f"/api/v1/projects/{project['id']}/models", headers=auth_headers).json()
    web = next(m for m in models if m["format"] == "glb")
    out = client.get(f"/api/v1/models/{web['id']}", headers=auth_headers).json()
    assets = out["texture_assets"]
    assert assets is not None
    assert assets["texture_count"] == 0 and assets["embedded_textures"] is False
    assert assets["primitives_total"] > 0
