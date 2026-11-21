"""阶段 D1/D2：通用局部更新（任意新增照片注册 + 局部融合候选）。"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from app.reconstruction.local_general import MAX_NEW_IMAGES, _select_dense_views, new_photo_name
from app.reconstruction.local_reconstruction import _poses


def _camera_images_bin(path: Path, names: list[str]):
    """最小合法 images.bin（单位四元数，0 观测）。"""
    with path.open("wb") as f:
        f.write(struct.pack("<Q", len(names)))
        for i, name in enumerate(names, start=1):
            f.write(struct.pack("<i7di", i, 1, 0, 0, 0, 0, 0, 0, 1))
            f.write(name.encode() + b"\0")
            f.write(struct.pack("<Q", 0))


def test_new_photo_name_is_deterministic_and_sanitized():
    assert new_photo_name(71, "DSC_1290.JPG") == "new_000071_DSC_1290.jpg"
    assert new_photo_name(5, "weird name!.png") == "new_000005_weird_name_.png"


def test_select_dense_views_takes_nearest_parents_and_all_new(tmp_path):
    names = ["p1", "p2", "p3", "p4", "n1"]
    _camera_images_bin(tmp_path / "images.bin", names)
    # 构造相机中心：把 t 设为 -center（R=I → center=-t）
    with (tmp_path / "images.bin").open("wb") as f:
        centers = {"p1": (0, 0, 0), "p2": (10, 0, 0), "p3": (20, 0, 0), "p4": (30, 0, 0),
                   "n1": (1, 1, 1)}
        f.write(struct.pack("<Q", len(centers)))
        for i, (name, c) in enumerate(centers.items(), start=1):
            f.write(struct.pack("<i7di", i, 1, 0, 0, 0, -c[0], -c[1], -c[2], 1))
            f.write(name.encode() + b"\0")
            f.write(struct.pack("<Q", 0))
    poses = _poses(tmp_path / "images.bin")
    lo, hi = np.array([8.0, -1.0, -1.0]), np.array([12.0, 1.0, 1.0])
    views = _select_dense_views(poses, ["n1"], lo, hi)
    # 最近 K 张保底（K=16）：全部父视角入选，按距选区中心排序
    assert "n1" in views and all(p in views for p in ("p1", "p2", "p3", "p4"))
    assert views.index("p2") < views.index("p1") and views.index("p2") < views.index("p3")
    assert views.index("p4") > views.index("p2")


class TestGeneralSubmission:
    def _setup_parent(self, client, auth_headers, project, upload_images, monkeypatch, tmp_path):
        """上传照片、用 mock 任务生成父模型，并伪造稀疏/点云检查点。"""
        import time as _time

        from app.core.config import get_settings
        from app.db.session import SessionLocal
        from app.models.entities import Model, ReconstructionJob
        from tests.test_jobs import wait_job

        upload_images(3)
        job = client.post(f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast", "enable_mesh": False},
                          headers=auth_headers).json()
        wait_job(client, auth_headers, job["id"])
        _time.sleep(0.2)
        with SessionLocal() as db:
            model_row = db.query(Model).filter_by(project_id=project["id"]).first()
            assert model_row is not None, "mock 任务应登记父模型"
            parent_job = db.get(ReconstructionJob, model_row.job_id)
            parent_manifest = json.loads(parent_job.input_manifest)
        monkeypatch.setattr(__import__("app.services.job_service", fromlist=["enqueue_job"]),
                            "enqueue_job", lambda job_id: None)
        base = (Path(get_settings().scratch_dir) / "users" / str(project["owner_id"]) / "projects"
                / str(project["id"]) / "jobs" / str(model_row.job_id) / "checkpoints")
        sparse = base / "SPARSE_RECONSTRUCTION" / "model"
        sparse.mkdir(parents=True, exist_ok=True)
        _camera_images_bin(sparse / "images.bin", [f"img_{i:04d}_view_{i:03d}.jpg" for i in range(3)])
        (sparse / "cameras.bin").write_bytes(struct.pack("<Q", 0))
        (sparse / "points3D.bin").write_bytes(struct.pack("<Q", 0))
        meshing = base / "MESHING"
        meshing.mkdir(parents=True, exist_ok=True)
        (meshing / "fused-filtered.ply").write_bytes(b"ply\nfake parent cloud")
        features = base / "FEATURE_EXTRACTION"
        features.mkdir(parents=True, exist_ok=True)
        (features / "database.db").write_bytes(b"fake parent feature database")
        return model_row, parent_manifest

    def _save_region(self, client, auth_headers, project, model_id):
        r = client.post(f"/api/v1/models/{model_id}/regions",
                        json={"bbox_min": [0, 0, 0], "bbox_max": [2, 2, 2], "note": "局部更新选区"},
                        headers=auth_headers)
        assert r.status_code == 201, r.text
        return r.json()

    def test_general_local_reconstruction_freezes_inputs(self, client, auth_headers, project, upload_images, monkeypatch):
        """无种子时走通用路径：任意新增照片、冻结父稀疏与点云检查点。"""
        model_row, _ = self._setup_parent(client, auth_headers, project, upload_images, monkeypatch, None)
        region = self._save_region(client, auth_headers, project, model_row.id)
        from tests.conftest import make_image_bytes

        upload2 = client.post(
            f"/api/v1/projects/{project['id']}/images",
            files=[("files", (f"extra_{i}.jpg", make_image_bytes(seed=90 + i), "image/jpeg")) for i in range(2)],
            headers=auth_headers,
        )
        assert upload2.status_code == 201, upload2.text
        new_ids = [img["id"] for img in upload2.json()]

        r = client.post(f"/api/v1/models/{model_row.id}/local-reconstruction",
                        json={"region_id": region["id"], "image_ids": new_ids}, headers=auth_headers)
        assert r.status_code == 202, r.text
        job = r.json()
        assert job["pipeline_mode"] == "local_general"
        from app.db.session import SessionLocal
        from app.models.entities import ReconstructionJob

        with SessionLocal() as db:
            config = json.loads(db.get(ReconstructionJob, job["id"]).config_json)
        assert config["parent_model_id"] == model_row.id
        assert config["parent_sparse"]["images.bin"]["sha256"]
        assert len(config["parent_images"]) == 3
        assert config["resolved"]["use_gpu"] is True

    def test_general_submission_requires_parent_checkpoints(self, client, auth_headers, project, upload_images, monkeypatch):
        """父任务没有稀疏检查点时，提交必须给出明确原因而不是排队后失败。"""
        from app.core.config import get_settings
        from app.db.session import SessionLocal
        from app.models.entities import Model

        from tests.test_jobs import wait_job

        upload_images(4)
        job = client.post(f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers).json()
        wait_job(client, auth_headers, job["id"])
        import time as _time

        _time.sleep(0.2)
        with SessionLocal() as db:
            model_row = db.query(Model).filter_by(project_id=project["id"]).first()
            assert model_row is not None
        monkeypatch.setattr(__import__("app.services.job_service", fromlist=["enqueue_job"]),
                            "enqueue_job", lambda job_id: None)
        region = self._save_region(client, auth_headers, project, model_row.id)
        imgs = client.get(f"/api/v1/projects/{project['id']}/images", headers=auth_headers).json()
        r = client.post(f"/api/v1/models/{model_row.id}/local-reconstruction",
                        json={"region_id": region["id"], "image_ids": [imgs[0]["id"]]}, headers=auth_headers)
        assert r.status_code == 400, r.text
        assert "稀疏" in r.json()["message"] or "检查点" in r.json()["message"]

    def test_region_too_small_rejected(self, client, auth_headers, project, upload_images, monkeypatch):
        from app.db.session import SessionLocal
        from app.models.entities import Model

        model_row, _ = self._setup_parent(client, auth_headers, project, upload_images, monkeypatch, None)
        monkeypatch.setattr(__import__("app.services.job_service", fromlist=["enqueue_job"]),
                            "enqueue_job", lambda job_id: None)
        r = client.post(f"/api/v1/models/{model_row.id}/regions",
                        json={"bbox_min": [0, 0, 0], "bbox_max": [0.05, 0.05, 0.05], "note": "过小"},
                        headers=auth_headers)
        assert r.status_code == 201
        region = r.json()
        imgs = client.get(f"/api/v1/projects/{project['id']}/images", headers=auth_headers).json()
        r = client.post(f"/api/v1/models/{model_row.id}/local-reconstruction",
                        json={"region_id": region["id"], "image_ids": [imgs[0]["id"]]}, headers=auth_headers)
        assert r.status_code == 400
        assert "选区过小" in r.json()["message"]

    def test_local_general_rejects_resume(self, client, auth_headers, project, upload_images, monkeypatch):
        model_row, _ = self._setup_parent(client, auth_headers, project, upload_images, monkeypatch, None)
        region = self._save_region(client, auth_headers, project, model_row.id)
        imgs = client.get(f"/api/v1/projects/{project['id']}/images", headers=auth_headers).json()
        monkeypatch.setattr(__import__("app.services.job_service", fromlist=["enqueue_job"]),
                            "enqueue_job", lambda job_id: None)
        job = client.post(f"/api/v1/models/{model_row.id}/local-reconstruction",
                          json={"region_id": region["id"], "image_ids": [imgs[0]["id"]]}, headers=auth_headers).json()
        r = client.post(f"/api/v1/jobs/{job['id']}/resume", headers=auth_headers)
        assert r.status_code == 409

    def test_image_ids_beyond_limit_rejected_by_schema(self, client, auth_headers, project, upload_images, monkeypatch):
        from app.db.session import SessionLocal
        from app.models.entities import Model

        model_row, _ = self._setup_parent(client, auth_headers, project, upload_images, monkeypatch, None)
        region = self._save_region(client, auth_headers, project, model_row.id)
        too_many = list(range(1, MAX_NEW_IMAGES + 2))
        r = client.post(f"/api/v1/models/{model_row.id}/local-reconstruction",
                        json={"region_id": region["id"], "image_ids": too_many}, headers=auth_headers)
        assert r.status_code == 422
