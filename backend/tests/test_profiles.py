"""集成 A1：配置冻结、auto 匹配决策与提交预检/能力门槛。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.reconstruction.profiles import (PROFILES, auto_matcher_decision, gpu_capability_report,
                                         parse_gpu_index, resolve_job_config)


def test_auto_matcher_detects_continuous_capture_order():
    names = [f"DSC_{i:04d}.JPG" for i in range(40)]
    matcher, reason = auto_matcher_decision(names)
    assert matcher == "sequential" and "连续编号" in reason


def test_auto_matcher_falls_back_to_exhaustive_for_unordered_sets():
    names = ["bridge.jpg", "roof.jpg", "window.jpg", "door.jpg", "sky.jpg", "wall.jpg"]
    matcher, reason = auto_matcher_decision(names)
    assert matcher == "exhaustive" and "无序" in reason


def test_auto_matcher_gapped_numbering_is_not_a_sequence():
    names = [f"DSC_{i:04d}.JPG" for i in (1, 2, 3, 4, 5, 25)]
    assert auto_matcher_decision(names)[0] == "exhaustive"


def test_speed_profile_freezes_combined_settings():
    """speed 画像是通用可选画像：显式画像参数优先于全局环境变量。"""
    resolved = resolve_job_config("speed", "auto", True, True, "colmap",
                                  [f"DSC_{i:04d}.JPG" for i in range(40)])["resolved"]
    assert resolved["patchmatch_iterations"] == 3
    assert resolved["fusion_threads"] == 16
    assert resolved["texture_start_scale"] == 0.125
    assert resolved["matcher"] == "sequential"
    assert resolved["poisson_depth"] == PROFILES["speed"]["poisson_depth"]


def test_standard_profile_uses_env_defaults_and_records_decision():
    out = resolve_job_config("standard", "auto", True, True, "mock", ["a.jpg", "b.jpg", "c.jpg"])
    assert out["resolved"]["matcher"] == "exhaustive"
    assert out["resolved"]["patchmatch_iterations"] == 5
    assert out["matcher_decision"] is not None


def test_parse_gpu_index():
    assert parse_gpu_index("0,1") == [0, 1]
    assert parse_gpu_index("0") == [0]


def test_gpu_capability_report_requires_all_requested_gpus(monkeypatch):
    import app.reconstruction.profiles as profiles

    monkeypatch.setattr(profiles, "_visible_gpu_count", lambda: 1)
    monkeypatch.setattr("app.reconstruction.adapter.colmap_has_cuda", lambda _bin: True)
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "colmap_gpu_index", "0,1", raising=False)
    report = gpu_capability_report("colmap")
    assert report["colmap_has_cuda"] is True
    assert report["requested_gpu_index"] == [0, 1]
    assert report["visible_gpu_count"] == 1
    assert report["all_requested_gpus_visible"] is False

    monkeypatch.setattr(get_settings(), "colmap_gpu_index", "0", raising=False)
    assert gpu_capability_report("colmap")["all_requested_gpus_visible"] is True


def test_gpu_visibility_unknown_when_probe_missing(monkeypatch):
    import app.reconstruction.profiles as profiles

    monkeypatch.setattr(profiles, "_visible_gpu_count", lambda: None)
    monkeypatch.setattr("app.reconstruction.adapter.colmap_has_cuda", lambda _bin: True)
    assert gpu_capability_report("colmap")["all_requested_gpus_visible"] is None


def test_frozen_resolved_survives_env_drift(tmp_path, monkeypatch):
    """执行上下文优先读任务冻结的 resolved，不受运行环境变量变化影响。"""
    from app.reconstruction.pipeline import PipelineContext
    from app.services import image_service

    monkeypatch.setattr(image_service, "project_job_dir", lambda *_: tmp_path / "job")
    monkeypatch.setattr(image_service, "project_output_dir", lambda *_: tmp_path / "out")
    config = {"preset": "speed", "matcher": "auto", "enable_dense": True, "mode": "colmap",
              **resolve_job_config("speed", "auto", True, True, "colmap",
                                   [f"DSC_{i:04d}.JPG" for i in range(40)])}
    ctx = PipelineContext(SimpleNamespace(id=1, pipeline_mode="colmap", engine_version=None),
                          SimpleNamespace(id=1, owner_id=1), config, {}, None)
    assert ctx.params["max_features"] == 8192
    assert ctx.patchmatch_iterations == 3
    assert ctx.fusion_threads == 16
    assert ctx.texture_start_scale == 0.125
    assert ctx.poisson_threads == 4


def test_legacy_config_without_resolved_keeps_env_behavior(tmp_path, monkeypatch):
    """旧任务（无 resolved）退回环境变量与 PRESET_PARAMS，行为与历史一致。"""
    from app.reconstruction.pipeline import PipelineContext
    from app.services import image_service

    monkeypatch.setattr(image_service, "project_job_dir", lambda *_: tmp_path / "job")
    monkeypatch.setattr(image_service, "project_output_dir", lambda *_: tmp_path / "out")
    ctx = PipelineContext(SimpleNamespace(id=1, pipeline_mode="colmap", engine_version=None),
                          SimpleNamespace(id=1, owner_id=1), {"preset": "fast"}, {}, None)
    assert ctx.params["max_features"] == 4096
    assert ctx.params["poisson_depth"] == 8
    assert ctx.params["matcher_default"] == "sequential"
    assert ctx.patchmatch_iterations == 5


class TestSubmissionGates:
    def test_create_job_freezes_resolved_config_and_preflight(self, client, auth_headers, project, upload_images):
        upload_images(5)
        r = client.post(f"/api/v1/projects/{project['id']}/jobs",
                        json={"preset": "speed", "matcher": "auto"}, headers=auth_headers)
        assert r.status_code == 202, r.text
        config = r.json()["config"]
        assert config["preset"] == "speed"
        assert config["resolved"]["patchmatch_iterations"] == 3
        assert config["resolved"]["fusion_threads"] == 16
        assert config["resolved"]["texture_start_scale"] == 0.125
        assert config["preflight"]["image_count"] == 5
        assert config["preflight"]["view_overlap"] == "unknown_until_registration"
        assert config["capability"] is None  # mock 模式不需要 GPU
        assert config["matcher_decision"]  # 5 张连续命名不足序列阈值 → 记录决策

    def test_missing_input_file_fails_at_submission(self, client, auth_headers, project, upload_images):
        from app.db.session import SessionLocal
        from app.models.entities import Image

        upload_images(4)
        with SessionLocal() as db:
            from sqlalchemy import select

            img = db.scalars(
                select(Image).where(Image.project_id == project["id"]).limit(1)
            ).first()
            Path(img.stored_path).unlink()
        r = client.post(f"/api/v1/projects/{project['id']}/jobs",
                        json={"preset": "standard"}, headers=auth_headers)
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "INPUT_FILES_MISSING"

    def test_dense_requires_cuda_colmap(self, client, auth_headers, project, upload_images, monkeypatch):
        from app.services import job_service

        upload_images(4)
        monkeypatch.setattr(job_service, "_resolve_mode", lambda _requested: "colmap")
        import app.reconstruction.profiles as profiles

        monkeypatch.setattr(profiles, "gpu_capability_report", lambda _bin: {
            "colmap_has_cuda": False, "requested_gpu_index": [0], "visible_gpu_count": None,
            "all_requested_gpus_visible": None,
        })
        r = client.post(f"/api/v1/projects/{project['id']}/jobs",
                        json={"preset": "standard", "enable_dense": True}, headers=auth_headers)
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "CUDA_UNAVAILABLE"
        assert "CUDA" in r.json()["message"]

    def test_dense_rejects_invisible_requested_gpu(self, client, auth_headers, project, upload_images, monkeypatch):
        from app.services import job_service

        upload_images(4)
        monkeypatch.setattr(job_service, "_resolve_mode", lambda _requested: "colmap")
        import app.reconstruction.profiles as profiles

        monkeypatch.setattr(profiles, "gpu_capability_report", lambda _bin: {
            "colmap_has_cuda": True, "requested_gpu_index": [0, 1], "visible_gpu_count": 1,
            "all_requested_gpus_visible": False,
        })
        r = client.post(f"/api/v1/projects/{project['id']}/jobs",
                        json={"preset": "standard", "enable_dense": True}, headers=auth_headers)
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "GPU_INDEX_UNAVAILABLE"

    def test_sparse_only_job_skips_gpu_gate(self, client, auth_headers, project, upload_images, monkeypatch):
        """enable_dense=False 的稀疏任务不要求 CUDA。"""
        from app.services import job_service

        upload_images(4)
        monkeypatch.setattr(job_service, "_resolve_mode", lambda _requested: "colmap")
        # 测试环境无 COLMAP 可执行文件；本测试只验证提交门槛，不入队执行。
        monkeypatch.setattr(job_service, "enqueue_job", lambda job_id: None)
        r = client.post(f"/api/v1/projects/{project['id']}/jobs",
                        json={"preset": "standard", "enable_dense": False}, headers=auth_headers)
        assert r.status_code == 202, r.text
        config = json.loads(r.content.decode("utf-8"))["config"] if isinstance(r.json().get("config"), str) else r.json()["config"]
        assert config["capability"] is None
        assert config["enable_dense"] is False
