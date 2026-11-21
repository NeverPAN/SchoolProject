import json
import struct
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import trimesh
from PIL import Image
from scipy.spatial.transform import Rotation

from app.reconstruction import pipeline, texturer
from app.reconstruction.photo_export import PHOTO_FILENAME, PHOTO_NATIVE, PHOTO_STAGE, photo_mesh_to_glb


@pytest.fixture
def meshes(tmp_path):
    source, baked = tmp_path / "source.ply", tmp_path / "baked.ply"
    header = "ply\nformat ascii 1.0\n"
    geometry = (
        "element vertex 4\nproperty float x\nproperty float y\nproperty float z\n"
        "element face 2\nproperty list uchar int vertex_indices\n"
    )
    vertices = "2 1 3\n3 1 3\n3 2 3\n2 2 3\n"
    source.write_text(header + geometry + "end_header\n" + vertices + "3 0 1 2\n3 0 2 3\n")
    baked.write_text(header + "comment TextureFile texture.png\n" + geometry
                     + "property list uchar float texcoord\nend_header\n" + vertices
                     + "3 0 1 2 6 0.2 0.2 0.8 0.2 0.8 0.8\n3 0 2 3 6 0 0 0 0 0 0\n")
    Image.new("RGB", (8, 4), (120, 80, 40)).save(tmp_path / "texture.png")
    return source, baked


@pytest.mark.parametrize("up", [None, [0, -1, 0]])
def test_glb_embeds_photo_atlas_and_preserves_face_coordinates(tmp_path, meshes, up):
    source, baked = meshes
    output = tmp_path / PHOTO_FILENAME
    source_bytes = source.read_bytes()
    metadata = photo_mesh_to_glb(source, baked, output, up)
    assert source.read_bytes() == source_bytes
    assert metadata["textured_faces"] == 1
    assert metadata["face_count"] == 2
    assert metadata["face_coverage"] == 0.5
    assert metadata["atlas_size"] == [8, 4]
    assert metadata["orientation"] == ("upright" if up else "native_manual")
    data = output.read_bytes()
    chunk_length, chunk_type = struct.unpack_from("<I4s", data, 12)
    assert chunk_type == b"JSON"
    tree = json.loads(data[20:20 + chunk_length])
    assert len(tree["images"]) == 1
    assert tree["images"][0]["mimeType"] == "image/png"
    assert "uri" not in tree["images"][0]
    assert sum("baseColorTexture" in mat["pbrMetallicRoughness"] for mat in tree["materials"]) == 1
    scene = trimesh.load_scene(output, process=False)
    native = trimesh.load_mesh(source, process=False)
    rotation = Rotation.from_quat(metadata["rotation_xyzw"])
    if up is not None:
        np.testing.assert_allclose(rotation.apply(up), [0, 1, 0], atol=1e-8)
    expected = rotation.apply((native.triangles - metadata["rotation_center"]).reshape(-1, 3)).reshape(-1, 3, 3)
    np.testing.assert_allclose(scene.geometry["photo"].triangles, expected[:1], atol=1e-6)
    np.testing.assert_allclose(scene.geometry["unobserved"].triangles, expected[1:], atol=1e-6)


@pytest.mark.parametrize("corrupt", ["winding", "atlas", "coverage"])
def test_exporter_rejects_bad_photo_artifacts(tmp_path, meshes, corrupt):
    source, baked = meshes
    text = baked.read_text()
    if corrupt == "winding":
        baked.write_text(text.replace("3 0 1 2 6", "3 0 2 1 6"))
    elif corrupt == "atlas":
        baked.write_text(text.replace("texture.png", "../texture.png"))
    else:
        baked.write_text(text.replace("0.2 0.2 0.8 0.2 0.8 0.8", "0 0 0 0 0 0"))
    output = tmp_path / PHOTO_FILENAME
    with pytest.raises(ValueError):
        photo_mesh_to_glb(source, baked, output, None)
    assert not output.exists()


def test_orientation_requires_vertical_support_and_independent_sign():
    down = "Found downward axis: 0 1 0"
    evidence = [f"=== Processing image {i}.jpg ({i} / 6) ===\n"
                "(19 horizontal inliers, 20 vertical inliers)\nVertical: 0 1 0"
                for i in range(1, 5)]
    log = "\n".join([*evidence, down])
    np.testing.assert_allclose(texturer._supported_up(log, [0, -1, 0]), [0, -1, 0])
    assert texturer._supported_up(log, [1, 0, 0]) is None
    assert texturer._supported_up("\n".join([evidence[0], down]), [0, -1, 0]) is None
    assert texturer._supported_up(log.replace("Vertical: 0 1 0", "Vertical: 1 0 0"), [0, -1, 0]) is None


@pytest.fixture
def stages(tmp_path):
    ctx = pipeline.PipelineContext.__new__(pipeline.PipelineContext)
    ctx.config = {}
    ctx.texture_start_scale = 0.5
    ctx.params = pipeline.PRESET_PARAMS["standard"]
    ctx.stage_fingerprints = {"IMAGE_UNDISTORTION": "undistortion", "MESHING": "meshing", "SPARSE_RECONSTRUCTION": "sparse"}
    ctx.job_inputs_sha = "inputs"
    ctx.checkpoints_dir = tmp_path / "checkpoints"
    ctx.out_dir = tmp_path / "out"
    ctx.out_dir.mkdir()
    ctx.current_stage = None
    ctx.cancel_check = lambda: False
    ctx.log_file = lambda stage: tmp_path / f"{stage}.log"
    ctx.job_log = lambda message: None
    ctx.set_stage_result = lambda *args, **kwargs: None
    ctx.set_stage_running = lambda stage: setattr(ctx, "current_stage", stage)
    adapter = SimpleNamespace(use_gpu=True, version="test")
    return pipeline.ColmapPipeline(ctx, adapter)


def test_baker_reduces_scale_for_oversized_photo_atlas(tmp_path, monkeypatch):
    from PIL import Image

    workspace, mesh, root = tmp_path / "dense", tmp_path / "mesh.ply", tmp_path / "outputs"
    (workspace / "images").mkdir(parents=True)
    (workspace / "sparse").mkdir()
    for name in ("cameras", "images", "points3D"):
        (workspace / "sparse" / f"{name}.bin").touch()
    mesh.touch()
    calls = []

    def run(self, args, log, callback, timeout_s):
        output = Path(args[args.index("--output_path") + 1])
        scale = float(args[args.index("--MeshTextureMapping.texture_scale_factor") + 1])
        calls.append(scale)
        (output / "texture.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", *( (16000, 12000) if len(calls) == 1 else (8000, 6000) )))
        (output / "mesh.ply").write_text("mesh")

    monkeypatch.setattr(texturer.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="--workspace_path --input_path --output_path --MeshTextureMapping.num_threads --MeshTextureMapping.texture_scale_factor --MeshTextureMapping.atlas_patch_padding"))
    monkeypatch.setattr(texturer.ColmapAdapter, "run_command", run)
    mesh_path = texturer.bake_texture("fake", workspace, mesh, root, root / "texture.log", lambda: None)
    assert calls == [0.5, 0.25]
    assert mesh_path == root / "selected" / "mesh.ply"
    assert struct.unpack(">II", (root / "selected" / "texture.png").read_bytes()[16:24]) == (8000, 6000)


@pytest.mark.parametrize("start_scale", [0.5, 0.125])
def test_baker_fails_closed_if_all_atlas_scales_are_oversized(tmp_path, monkeypatch, start_scale):
    from PIL import Image

    workspace, mesh, root = tmp_path / "dense", tmp_path / "mesh.ply", tmp_path / "outputs"
    (workspace / "images").mkdir(parents=True)
    (workspace / "sparse").mkdir()
    for name in ("cameras", "images", "points3D"):
        (workspace / "sparse" / f"{name}.bin").touch()
    mesh.touch()

    monkeypatch.setattr(texturer.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="--workspace_path --input_path --output_path --MeshTextureMapping.num_threads --MeshTextureMapping.texture_scale_factor --MeshTextureMapping.atlas_patch_padding"))

    attempts = []

    def run(self, args, log, callback, timeout_s):
        attempts.append(float(args[args.index("--MeshTextureMapping.texture_scale_factor") + 1]))
        output = Path(args[args.index("--output_path") + 1])
        (output / "texture.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", 14000, 13000))
        (output / "mesh.ply").write_text("mesh")

    monkeypatch.setattr(texturer.ColmapAdapter, "run_command", run)
    with pytest.raises(ValueError, match="尺寸上限"):
        texturer.bake_texture("fake", workspace, mesh, root, root / "texture.log", lambda: None, start_scale=start_scale)
    assert attempts == [scale for scale in texturer.TEXTURE_SCALES if scale <= start_scale]


@pytest.mark.parametrize("up,origin", [(None, PHOTO_NATIVE), ([0, -1, 0], PHOTO_STAGE)])
def test_texture_stage_registers_additive_artifact_and_reuses_verified_checkpoint(tmp_path, stages, meshes, monkeypatch, up, origin):
    source, baked = meshes
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(colmap_texturer_bin="colmap"))
    monkeypatch.setattr(texturer, "resolve_texturer", lambda name: str(source))
    bakes = []

    def bake(*args, start_scale):
        assert start_scale == 0.5
        bakes.append(args)
        return baked

    monkeypatch.setattr(texturer, "bake_texture", bake)
    monkeypatch.setattr(texturer, "estimate_upright", lambda *args: up)
    monkeypatch.setattr(pipeline, "_export_sparse_to_ply", lambda *args: None)
    result = {"warnings": []}
    glb = stages._stage_texturing(tmp_path, source, result)
    assert glb and glb.read_bytes()[:4] == b"glTF"
    stages._stage_export(tmp_path, None, source, result, photo_glb=glb)
    exports = stages.ctx.checkpoints_dir / "EXPORTING" / "exported.json"
    items = {item["path"]: item for item in json.loads(exports.read_text())["exported"]}
    assert items[PHOTO_FILENAME]["origin"] == origin
    assert items["model.glb"]["origin"] == "EXPORTING"
    assert (stages.ctx.out_dir / "mesh.ply").read_bytes() == source.read_bytes()
    second_result = {"warnings": []}
    assert stages._stage_texturing(tmp_path, source, second_result) == glb
    assert second_result == result
    assert len(bakes) == 1
    glb.write_bytes(b"corrupt")
    assert stages._stage_texturing(tmp_path, source, {"warnings": []}) == glb
    assert len(bakes) == 2


def test_texture_failure_keeps_baseline_and_never_registers_stale_photo(tmp_path, stages, meshes, monkeypatch):
    source, _ = meshes
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(colmap_texturer_bin="colmap"))
    monkeypatch.setattr(texturer, "resolve_texturer", lambda name: str(source))
    monkeypatch.setattr(pipeline, "_export_sparse_to_ply", lambda *args: None)
    stale = stages.ctx.out_dir / PHOTO_FILENAME
    stale.write_bytes(b"old")

    def unavailable(*args, **kwargs):
        raise texturer.TexturingUnavailable("no capability")

    monkeypatch.setattr(texturer, "bake_texture", unavailable)
    result = {"warnings": []}
    assert stages._stage_texturing(tmp_path, source, result) is None
    assert result["photo_texture"]["status"] == "fallback"
    assert result["warnings"]
    stages._stage_export(tmp_path, None, source, result)
    assert not stale.exists()
    assert (stages.ctx.out_dir / "model.glb").is_file()


def test_disabled_texture_does_not_probe_colmap(tmp_path, stages, meshes, monkeypatch):
    source, _ = meshes
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(colmap_texturer_bin=None))
    monkeypatch.setattr(texturer, "resolve_texturer", lambda _: pytest.fail("Unexpected texturer probe"))
    result = {"warnings": []}
    assert stages._stage_texturing(tmp_path, source, result) is None
    assert result["photo_texture"]["status"] == "skipped"
    assert result["warnings"] == []


def test_texturing_cancellation_is_not_fallback(tmp_path, stages, meshes, monkeypatch):
    source, _ = meshes
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(colmap_texturer_bin="colmap"))
    monkeypatch.setattr(texturer, "resolve_texturer", lambda name: str(source))

    def cancel(*args, **kwargs):
        raise pipeline.StageCancelled("TEXTURING")

    monkeypatch.setattr(texturer, "bake_texture", cancel)
    result = {"warnings": []}
    with pytest.raises(pipeline.StageCancelled):
        stages._stage_texturing(tmp_path, source, result)
    assert result == {"warnings": []}
