"""工具与 manifest（断点续跑依据）单元测试。"""
from __future__ import annotations

import json

import pytest


class TestFileUtils:
    def test_sanitize_filename(self):
        from app.utils.files import sanitize_filename

        assert sanitize_filename("../../etc/passwd.jpg") == "passwd.jpg"
        assert sanitize_filename("a/b/c.png") == "c.png"
        assert sanitize_filename("正常名称-1.jpg") == "正常名称-1.jpg"
        assert sanitize_filename("") == "unnamed"

    def test_safe_relpath_blocks_traversal(self):
        from app.utils.files import safe_relpath

        with pytest.raises(ValueError):
            safe_relpath("users", "..", "etc")
        with pytest.raises(ValueError):
            safe_relpath("/abs/path")

    def test_sha256(self, tmp_path):
        from app.utils.files import sha256_of_file

        f = tmp_path / "x.bin"
        f.write_bytes(b"hello world")
        assert sha256_of_file(f) == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


class TestManifest:
    def test_write_read(self, tmp_path):
        from app.reconstruction.manifest import read_manifest, write_manifest

        f = tmp_path / "out.bin"
        f.write_bytes(b"12345")
        m = write_manifest(tmp_path, "TEST_STAGE", {"p": 1}, {"i": 2}, ["out.bin"])
        assert m["stage"] == "TEST_STAGE"
        m2 = read_manifest(tmp_path)
        assert m2["params_sha"] == m["params_sha"]
        assert m2["files"][0]["sha256"]

    def test_checkpoint_valid_roundtrip(self, tmp_path):
        from app.reconstruction.manifest import compute_sha_of, stage_checkpoint_valid, write_manifest

        f = tmp_path / "out.bin"
        f.write_bytes(b"12345")
        params = {"preset": "fast", "stage": "X"}
        inputs = {"job_inputs_sha": "abc"}
        write_manifest(tmp_path, "X", params, inputs, ["out.bin"])
        assert stage_checkpoint_valid(tmp_path, "X", params, inputs)
        # 参数变化 → 失效
        assert not stage_checkpoint_valid(tmp_path, "X", {**params, "preset": "high"}, inputs)
        # 输入变化 → 失效
        assert not stage_checkpoint_valid(tmp_path, "X", params, {"job_inputs_sha": "zzz"})
        # 文件被改动 → 失效
        assert not stage_checkpoint_valid(tmp_path, "X", params, inputs, ["other.bin"])
        f.write_bytes(b"abcde")  # identical size, different contents
        assert not stage_checkpoint_valid(tmp_path, "X", params, inputs)

    def test_checkpoint_rejects_incomplete_outputs(self, tmp_path):
        from app.reconstruction.manifest import read_manifest, write_manifest

        with pytest.raises(ValueError, match="Missing or empty"):
            write_manifest(tmp_path, "X", {}, {}, ["missing.bin"])
        assert read_manifest(tmp_path) is None

    def test_input_hash_ignores_timestamp_but_not_image_identity(self):
        from app.reconstruction.manifest import input_manifest_sha

        data = {"generated_at": "now", "images": [{"image_id": 4, "checksum": "abc"}]}
        assert input_manifest_sha(data) == input_manifest_sha({**data, "generated_at": "later"})
        assert input_manifest_sha(data) != input_manifest_sha({**data, "images": [{"image_id": 5, "checksum": "abc"}]})

    def test_compute_sha_stable(self):
        from app.reconstruction.manifest import compute_sha_of

        a = compute_sha_of({"x": 1, "y": [1, 2]})
        b = compute_sha_of({"y": [1, 2], "x": 1})  # 键序无关
        assert a == b
        assert a != compute_sha_of({"x": 2, "y": [1, 2]})


def test_colmap_quiet_command_timeout_and_callback(tmp_path):
    import sys
    import time

    from app.reconstruction.adapter import ColmapAdapter, ColmapStageError
    from app.reconstruction.pipeline import StageCancelled

    runner = ColmapAdapter(sys.executable, False)
    log = tmp_path / "child.log"
    now = time.monotonic()
    with pytest.raises(ColmapStageError, match="超时"):
        runner.run_command(["-c", "import time; time.sleep(60)"], log, timeout_s=0.2)
    assert time.monotonic() - now < 5

    def cancel():
        if time.monotonic() - started > 0.1:
            raise StageCancelled("TEST")
        return False

    started = time.monotonic()
    with pytest.raises(StageCancelled):
        runner.run_command(["-c", "import time; time.sleep(60)"], log, cancel)
    assert time.monotonic() - started < 5


def test_adapter_passes_gpu_index_to_gpu_stages(tmp_path):
    """多卡支持：gpu_index 必须进入 SIFT 提取/匹配与稠密重建命令（traceability + 多卡调度）。"""
    from unittest.mock import patch

    import sys

    from app.reconstruction.adapter import ColmapAdapter

    captured: list[list[str]] = []

    def fake_run(self, args, log_file, cancel_check=None, timeout_s=10, success_hint=None):
        captured.append(args)

    # 3.13 的版本串来自 `colmap -h`，包含 "COLMAP" 前缀
    runner = ColmapAdapter(sys.executable, True, "COLMAP 3.13.0 -- Structure-from-Motion", gpu_index="0,1")
    with patch.object(ColmapAdapter, "run_command", fake_run):
        runner.feature_extraction(tmp_path / "db.db", tmp_path, tmp_path / "log")
        runner.feature_matching(tmp_path / "db.db", "exhaustive", tmp_path / "log")
        runner.dense_stereo(tmp_path / "dense", tmp_path / "log", max_image_size=2000, num_iterations=3)
        runner.stereo_fusion(tmp_path / "dense", tmp_path / "fused.ply", tmp_path / "log", num_threads=16)
    assert "--FeatureExtraction.gpu_index" in captured[0]
    assert captured[0][captured[0].index("--FeatureExtraction.gpu_index") + 1] == "0,1"
    assert "--FeatureMatching.gpu_index" in captured[1]
    assert "--PatchMatchStereo.gpu_index" in captured[2]
    assert captured[2][captured[2].index("--PatchMatchStereo.gpu_index") + 1] == "0,1"
    assert captured[2][captured[2].index("--PatchMatchStereo.num_iterations") + 1] == "3"
    assert captured[3][captured[3].index("--StereoFusion.num_threads") + 1] == "16"
    # 单卡默认值也必须显式传入，保证命令可追溯
    single = ColmapAdapter(sys.executable, True, "COLMAP 3.13.0")
    assert single.gpu_index == "0"


def test_dense_layout_requires_geometric_depth_and_normal_maps(tmp_path):
    from app.reconstruction.pipeline import _stereo_files, _undistortion_files

    paths = ["sparse/cameras.bin", "sparse/images.bin", "sparse/points3D.bin", "images/img.jpg",
             "stereo/patch-match.cfg", "stereo/fusion.cfg", "stereo/depth_maps/img.jpg.geometric.bin",
             "stereo/normal_maps/img.jpg.geometric.bin"]
    for rel in paths:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("img.jpg\n", encoding="utf-8")
    assert _undistortion_files(tmp_path)
    assert len(set(_stereo_files(tmp_path))) == 2
    (tmp_path / paths[-1]).unlink()
    assert not _stereo_files(tmp_path)


def test_colmap_resumes_canonical_sparse_model_and_isolates_databases(tmp_path, monkeypatch):
    import shutil
    import sqlite3
    from contextlib import closing
    from types import SimpleNamespace

    from app.reconstruction.manifest import sha256_file
    from app.reconstruction.pipeline import ColmapPipeline, PipelineContext, SPARSE_MODEL_FILES
    from app.services import image_service

    monkeypatch.setattr(image_service, "project_job_dir", lambda *_: tmp_path / "job")
    monkeypatch.setattr(image_service, "project_output_dir", lambda *_: tmp_path / "out")

    class Engine:
        use_gpu = False
        version = "COLMAP 3.13"
        calls = []

        def feature_extraction(self, path, *_args, max_features):
            self.calls.append("extract")
            assert max_features == 4096
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE extracted(x)")

        def feature_matching(self, path, matcher, *_args):
            self.calls.append("match")
            assert matcher == "sequential"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE matched(x)")

        def sparse_mapping(self, path, images, output, *_args):
            self.calls.append("map")
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE mapped(x)")
            model = output / "0"
            model.mkdir(parents=True)
            for name in SPARSE_MODEL_FILES:
                (model / name).write_bytes(b"fake model fixture")
            return model

    inputs = []
    for i in range(3):
        image = tmp_path / f"{i}.jpg"
        image.write_bytes(b"fake image fixture")
        inputs.append(dict(image_id=i, filename=image.name, stored_path=str(image), checksum=sha256_file(image)))

    def setup():
        ctx = PipelineContext(SimpleNamespace(id=2, pipeline_mode="colmap", engine_version=None),
                              SimpleNamespace(id=1, owner_id=1), {"preset": "fast"},
                              {"images": inputs}, None)
        monkeypatch.setattr(ctx, "set_stage_running", lambda stage: None)
        monkeypatch.setattr(ctx, "set_stage_result", lambda *args, **kwargs: None)
        work = ctx.work_dir / "work"
        work.mkdir(exist_ok=True)
        pipeline = ColmapPipeline(ctx, engine)
        pipeline._stage_image_validation()
        pipeline._stage_feature_extraction(work)
        return ctx, pipeline._stage_sparse(work), work

    engine = Engine()
    ctx, model, work = setup()
    assert model == ctx.checkpoints_dir / "SPARSE_RECONSTRUCTION" / "model"
    for stage, expected in (("FEATURE_EXTRACTION", ["extracted"]),
                            ("FEATURE_MATCHING", ["extracted", "matched"])):
        with closing(sqlite3.connect(ctx.checkpoints_dir / stage / "database.db")) as db:
            assert [r[0] for r in db.execute("SELECT name FROM sqlite_master ORDER BY name")] == expected
    shutil.rmtree(work / "sparse")
    assert setup()[1] == model
    assert engine.calls == ["extract", "match", "map"]
    engine.version = "COLMAP 3.14"
    assert setup()[1] == model
    assert engine.calls == ["extract", "match", "map"] * 2


class TestJobManifest:
    def test_input_manifest_tracks_images(self, client, auth_headers, project, upload_images):
        from app.db.session import SessionLocal
        from app.models.entities import Project
        from app.services.image_service import build_input_manifest

        upload_images(3)
        with SessionLocal() as db:
            p = db.get(Project, project["id"])
            m = build_input_manifest(db, p)
        assert m["image_count"] == 3
        assert len(m["images"]) == 3
        assert all(len(i["checksum"]) == 64 for i in m["images"])
