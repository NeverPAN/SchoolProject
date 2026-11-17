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
        f.write_bytes(b"changed!")
        assert not stage_checkpoint_valid(tmp_path, "X", params, inputs)

    def test_compute_sha_stable(self):
        from app.reconstruction.manifest import compute_sha_of

        a = compute_sha_of({"x": 1, "y": [1, 2]})
        b = compute_sha_of({"y": [1, 2], "x": 1})  # 键序无关
        assert a == b
        assert a != compute_sha_of({"x": 2, "y": [1, 2]})


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
