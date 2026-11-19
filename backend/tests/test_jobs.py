"""重建任务全生命周期测试（Mock 管线 — 覆盖状态机/阶段事件/取消/恢复/模型登记）。"""
from __future__ import annotations

import time


def wait_job(client, headers, job_id, statuses=("SUCCEEDED", "FAILED", "CANCELLED"), timeout=90):
    t0 = time.time()
    job = None
    while time.time() - t0 < timeout:
        job = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
        if job["status"] in statuses:
            return job
        time.sleep(0.2)
    return job


class TestJobLifecycle:
    def test_full_success_flow(self, client, auth_headers, project, upload_images):
        upload_images(5)
        r = client.post(
            f"/api/v1/projects/{project['id']}/jobs",
            json={"preset": "fast", "enable_dense": True, "enable_mesh": True},
            headers=auth_headers,
        )
        assert r.status_code == 202, r.text
        job = r.json()
        assert job["status"] == "QUEUED"
        assert job["pipeline_mode"] == "mock"

        job = wait_job(client, auth_headers, job["id"])
        assert job["status"] == "SUCCEEDED", job.get("error_message")
        assert job["progress"] == 100.0

        # 阶段事件完整且可追踪（不允许只有一个"运行中"）
        events = client.get(f"/api/v1/jobs/{job['id']}/events", headers=auth_headers).json()
        stages = [e["stage"] for e in events]
        assert stages[0] == "IMAGE_VALIDATION"
        assert stages[-1] == "EXPORTING"
        assert all(e["status"] in ("completed", "skipped") for e in events)
        assert all(e["duration_ms"] is not None for e in events if e["status"] == "completed")

        # 日志可查询
        logs = client.get(f"/api/v1/jobs/{job['id']}/logs", headers=auth_headers)
        assert logs.status_code == 200
        assert "开始执行" in logs.text

    def test_models_registered_and_downloadable(self, client, auth_headers, project, upload_images):
        upload_images(4)
        job = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        job = wait_job(client, auth_headers, job["id"])
        assert job["status"] == "SUCCEEDED"

        models = client.get(f"/api/v1/projects/{project['id']}/models", headers=auth_headers).json()
        assert len(models) >= 2  # 点云 + web glb
        types = {m["model_type"] for m in models}
        assert "sparse_pointcloud" in types
        assert "web" in types
        for m in models:
            assert m["file_size"] > 0
        # 下载
        web = next(m for m in models if m["model_type"] == "web")
        r = client.get(f"/api/v1/models/{web['id']}/download", headers=auth_headers)
        assert r.status_code == 200
        assert len(r.content) == web["file_size"]
        assert r.content[:4] == b"glTF"  # GLB 魔数
        # 版本记录
        detail = client.get(f"/api/v1/models/{web['id']}", headers=auth_headers).json()
        assert detail["versions"][0]["version_number"] == 1

    def test_too_few_images_rejected(self, client, auth_headers, project, upload_images):
        upload_images(2)
        r = client.post(f"/api/v1/projects/{project['id']}/jobs", json={}, headers=auth_headers)
        assert r.status_code == 400

    def test_concurrent_job_rejected(self, client, auth_headers, project, upload_images, monkeypatch):
        """同一项目不允许同时两个任务。"""
        upload_images(4)
        monkeypatch.setenv("MOCK_DELAY_SCALE", "6")
        r1 = client.post(f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers)
        assert r1.status_code == 202
        deadline = time.time() + 15
        while time.time() < deadline:
            j = client.get(f"/api/v1/jobs/{r1.json()['id']}", headers=auth_headers).json()
            if j["status"] == "RUNNING":
                break
            time.sleep(0.1)
        r2 = client.post(f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers)
        assert r2.status_code == 409
        client.post(f"/api/v1/jobs/{r1.json()['id']}/cancel", headers=auth_headers)
        wait_job(client, auth_headers, r1.json()["id"], timeout=180)

    def test_cancel_running_job(self, client, auth_headers, project, upload_images, monkeypatch):
        upload_images(4)
        monkeypatch.setenv("MOCK_DELAY_SCALE", "5")
        job = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        deadline = time.time() + 15
        while time.time() < deadline:
            j = client.get(f"/api/v1/jobs/{job['id']}", headers=auth_headers).json()
            if j["status"] == "RUNNING":
                break
            time.sleep(0.05)
        r = client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=auth_headers)
        assert r.status_code == 200
        final = wait_job(client, auth_headers, job["id"], timeout=180)
        assert final["status"] == "CANCELLED"
        events = client.get(f"/api/v1/jobs/{job['id']}/events", headers=auth_headers).json()
        assert any(e["status"] == "cancelled" for e in events)

    def test_cancel_queued_job_directly(self, client, auth_headers, project, upload_images, monkeypatch):
        """未开始执行的任务直接标记取消。"""
        upload_images(4)
        monkeypatch.setattr("app.services.job_service.enqueue_job", lambda job_id: None)
        job = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        r = client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "CANCELLED"


class TestResume:
    def test_resume_after_failure_skips_completed_stages(
        self, client, auth_headers, project, upload_images, monkeypatch
    ):
        """失败 → 恢复：已完成阶段的检查点被复用，最终成功。"""
        from app.reconstruction.mock_pipeline import MockPipeline
        from app.reconstruction.pipeline import PipelineError

        upload_images(4)
        original_export = MockPipeline._export

        def boom(self, ctx):
            raise PipelineError("EXPORTING", "TEST_FAIL", "模拟导出失败")

        monkeypatch.setenv("MOCK_DELAY_SCALE", "0.01")
        monkeypatch.setattr(MockPipeline, "_export", boom)
        job = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        failed = wait_job(client, auth_headers, job["id"])
        assert failed["status"] == "FAILED"
        assert failed["error_stage"] == "EXPORTING"
        assert failed["error_code"] == "TEST_FAIL"

        # 解除故障注入后恢复
        monkeypatch.setattr(MockPipeline, "_export", original_export)
        r = client.post(f"/api/v1/jobs/{job['id']}/resume", headers=auth_headers)
        assert r.status_code == 200
        resumed = wait_job(client, auth_headers, job["id"])
        assert resumed["status"] == "SUCCEEDED", resumed.get("error_message")

        events = client.get(f"/api/v1/jobs/{job['id']}/events", headers=auth_headers).json()
        checkpoint_skips = [
            e for e in events if e["status"] == "skipped" and e["message"] and "检查点" in e["message"]
        ]
        assert len(checkpoint_skips) >= 3  # 前序阶段命中检查点，未重复执行

    def test_resume_rejected_when_inputs_changed(
        self, client, auth_headers, project, upload_images, monkeypatch
    ):
        """输入图片变化后不允许恢复旧任务（防止错误复用旧结果）。"""
        from app.reconstruction.mock_pipeline import MockPipeline
        from app.reconstruction.pipeline import PipelineError
        from tests.conftest import make_image_bytes

        upload_images(4)
        original_export = MockPipeline._export

        def boom(self, ctx):
            raise PipelineError("EXPORTING", "TEST_FAIL", "模拟导出失败")

        monkeypatch.setenv("MOCK_DELAY_SCALE", "0.01")
        monkeypatch.setattr(MockPipeline, "_export", boom)
        job = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        failed = wait_job(client, auth_headers, job["id"])
        assert failed["status"] == "FAILED"

        # 改变输入（新增图片）
        client.post(
            f"/api/v1/projects/{project['id']}/images",
            files=[("files", ("extra.jpg", make_image_bytes(seed=777), "image/jpeg"))],
            headers=auth_headers,
        )
        r = client.post(f"/api/v1/jobs/{job['id']}/resume", headers=auth_headers)
        assert r.status_code == 409
        assert r.json()["code"] == "INPUT_CHANGED"

    def test_resume_only_allowed_on_failed_or_cancelled(self, client, auth_headers, project, upload_images):
        upload_images(4)
        job = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        finished = wait_job(client, auth_headers, job["id"])
        assert finished["status"] == "SUCCEEDED"
        r = client.post(f"/api/v1/jobs/{job['id']}/resume", headers=auth_headers)
        assert r.status_code == 409


def test_publication_rolls_back_versions_and_models(client, auth_headers, project, upload_images, monkeypatch):
    from app.db.session import SessionLocal
    from app.models.entities import Model, ModelVersion
    from app.services import model_service
    from sqlalchemy import func, select

    upload_images(3)
    ensure_version = model_service.ensure_version

    def fail_after_insert(db, model, **kwargs):
        ensure_version(db, model, **kwargs)
        db.flush()
        raise OSError("injected publication failure")

    monkeypatch.setattr(model_service, "ensure_version", fail_after_insert)
    job = client.post(f"/api/v1/projects/{project['id']}/jobs", json={}, headers=auth_headers).json()
    failed = wait_job(client, auth_headers, job["id"])
    assert failed["status"] == "FAILED"
    with SessionLocal() as db:
        assert not db.scalar(select(func.count()).select_from(Model).filter_by(job_id=job["id"]))
        assert not db.scalar(select(func.count()).select_from(ModelVersion).filter_by(project_id=project["id"]))


def test_cancel_at_publication_does_not_register_models(client, auth_headers, project, upload_images, monkeypatch):
    from app.db.session import SessionLocal
    from app.models.entities import ReconstructionJob
    from app.services import job_service, model_service

    upload_images(3)
    prepare = model_service.prepare_job_models

    def cancel_during_prepare(job):
        records = prepare(job)
        with SessionLocal() as db:
            job_service.request_cancel(db, db.get(ReconstructionJob, job.id))
        return records

    monkeypatch.setattr(model_service, "prepare_job_models", cancel_during_prepare)
    job = client.post(f"/api/v1/projects/{project['id']}/jobs", json={}, headers=auth_headers).json()
    final = wait_job(client, auth_headers, job["id"])
    assert final["status"] == "CANCELLED"
    assert client.get(f"/api/v1/projects/{project['id']}/models", headers=auth_headers).json() == []


def test_stale_worker_closes_events_but_preserves_live_lock(client, auth_headers, project, upload_images, monkeypatch):
    from types import SimpleNamespace

    import redis
    import rq
    from rq.job import Job

    from app.db.base import utcnow_db
    from app.db.session import SessionLocal
    from app.models.entities import JobStageEvent, ReconstructionJob
    from app.services import image_service, job_service

    upload_images(3)
    monkeypatch.setattr(job_service, "enqueue_job", lambda job_id: None)
    job_id = client.post(f"/api/v1/projects/{project['id']}/jobs", json={}, headers=auth_headers).json()["id"]
    with SessionLocal() as db:
        job = db.get(ReconstructionJob, job_id)
        job.status = "RUNNING"
        job.current_stage = "MESHING"
        job.started_at = utcnow_db()
        db.add(JobStageEvent(job_id=job_id, stage="MESHING", status="running", started_at=job.started_at))
        work_dir = image_service.project_job_dir(job.project, job_id)
        db.commit()

    registry = SimpleNamespace(get_expired_job_ids=lambda: [], get_job_ids=lambda **_: ["rq-execution-uuid"])
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_: object())
    monkeypatch.setattr(rq, "Queue", lambda *_, **__: SimpleNamespace(jobs=[], started_job_registry=registry))
    monkeypatch.setattr(Job, "fetch_many", lambda ids, **_: [SimpleNamespace(args=(job_id,)) for _ in ids])
    with job_service.execution_lock(work_dir) as acquired:
        assert acquired
        job_service.requeue_stuck_jobs()
        with SessionLocal() as db:
            assert db.get(ReconstructionJob, job_id).status == "RUNNING"

    job_service.requeue_stuck_jobs()
    with SessionLocal() as db:
        job = db.get(ReconstructionJob, job_id)
        assert (job.status, job.error_code) == ("FAILED", "WORKER_LOST")
        assert job.stage_events[0].status == "failed"
        assert job.stage_events[0].finished_at is not None


class TestAppendPhotoVersioning:
    def test_append_photos_continues_version_chain(self, client, auth_headers, project, upload_images):
        """阶段 7/E6：追加照片 → 新模型版本续接编号并指向父版本；旧模型保持不变。"""
        upload_images(4)
        job1 = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        job1 = wait_job(client, auth_headers, job1["id"])
        assert job1["status"] == "SUCCEEDED", job1.get("error_message")

        models = client.get(f"/api/v1/projects/{project['id']}/models", headers=auth_headers).json()
        web1 = next(m for m in models if m["model_type"] == "web")
        old_bytes = client.get(f"/api/v1/models/{web1['id']}/download", headers=auth_headers).content
        v1 = client.get(f"/api/v1/models/{web1['id']}", headers=auth_headers).json()["versions"][0]

        upload_images(3, start_seed=100)
        job2 = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        job2 = wait_job(client, auth_headers, job2["id"])
        assert job2["status"] == "SUCCEEDED", job2.get("error_message")

        models = client.get(f"/api/v1/projects/{project['id']}/models", headers=auth_headers).json()
        webs = [m for m in models if m["model_type"] == "web"]
        assert len(webs) == 2
        web2 = next(m for m in webs if m["job_id"] == job2["id"])
        versions = client.get(f"/api/v1/models/{web2['id']}", headers=auth_headers).json()["versions"]
        assert versions[0]["version_number"] == 2
        assert versions[0]["change_type"] == "append_images"
        assert versions[0]["parent_version_id"] == v1["id"]
        assert "追加 3 张" in versions[0]["change_description"]

        # 旧模型文件与版本不受影响；新模型可下载
        assert client.get(f"/api/v1/models/{web1['id']}/download", headers=auth_headers).content == old_bytes
        v1_after = client.get(f"/api/v1/models/{web1['id']}", headers=auth_headers).json()["versions"]
        assert len(v1_after) == 1 and v1_after[0]["version_number"] == 1
        new_bytes = client.get(f"/api/v1/models/{web2['id']}/download", headers=auth_headers)
        assert new_bytes.status_code == 200 and new_bytes.content[:4] == b"glTF"

    def test_rerun_without_new_photos_marks_rerun(self, client, auth_headers, project, upload_images):
        upload_images(4)
        job1 = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        job1 = wait_job(client, auth_headers, job1["id"])
        assert job1["status"] == "SUCCEEDED"

        job2 = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        job2 = wait_job(client, auth_headers, job2["id"])
        assert job2["status"] == "SUCCEEDED"
        models = client.get(f"/api/v1/projects/{project['id']}/models", headers=auth_headers).json()
        web2 = next(m for m in models if m["model_type"] == "web" and m["job_id"] == job2["id"])
        versions = client.get(f"/api/v1/models/{web2['id']}", headers=auth_headers).json()["versions"]
        assert versions[0]["version_number"] == 2
        assert versions[0]["change_type"] == "rerun"


class TestRegionPrototype:
    def test_region_select_and_local_update(self, client, auth_headers, project, upload_images):
        """阶段 8 原型：保存框选区域 → 区域提取任务 → 登记 LOCAL_REGION 模型。"""
        upload_images(4)
        job1 = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        job1 = wait_job(client, auth_headers, job1["id"])
        assert job1["status"] == "SUCCEEDED", job1.get("error_message")

        models = client.get(f"/api/v1/projects/{project['id']}/models", headers=auth_headers).json()
        web = next(m for m in models if m["model_type"] == "web")

        r = client.post(
            f"/api/v1/models/{web['id']}/regions",
            json={"bbox_min": [-100, -100, -100], "bbox_max": [100, 100, 100],
                  "camera": {"position": [1, 1, 1]}, "note": "测试选区"},
            headers=auth_headers,
        )
        assert r.status_code == 201, r.text
        region = r.json()
        assert region["id"] > 0

        listed = client.get(f"/api/v1/models/{web['id']}/regions", headers=auth_headers).json()
        assert any(x["id"] == region["id"] for x in listed)

        job2 = client.post(
            f"/api/v1/models/{web['id']}/local-update",
            json={"region_id": region["id"]},
            headers=auth_headers,
        )
        assert job2.status_code == 202, job2.text
        job2 = wait_job(client, auth_headers, job2.json()["id"])
        assert job2["status"] == "SUCCEEDED", job2.get("error_message")

        models = client.get(f"/api/v1/projects/{project['id']}/models", headers=auth_headers).json()
        local = next(m for m in models if m.get("stage_origin") == "LOCAL_REGION")
        assert local["model_type"] == "mesh" and local["face_count"] > 0
        blob = client.get(f"/api/v1/models/{local['id']}/download", headers=auth_headers)
        assert blob.status_code == 200 and blob.content[:4] == b"glTF"
        manifest = client.get(f"/api/v1/models/{local['id']}/manifest", headers=auth_headers).json()
        assert "非局部重建" in manifest["manifest"]["note"]

    def test_region_invalid_bbox_rejected(self, client, auth_headers, project, upload_images):
        upload_images(4)
        job = client.post(
            f"/api/v1/projects/{project['id']}/jobs", json={"preset": "fast"}, headers=auth_headers
        ).json()
        job = wait_job(client, auth_headers, job["id"])
        assert job["status"] == "SUCCEEDED"
        models = client.get(f"/api/v1/projects/{project['id']}/models", headers=auth_headers).json()
        web = next(m for m in models if m["model_type"] == "web")
        r = client.post(
            f"/api/v1/models/{web['id']}/regions",
            json={"bbox_min": [5, 5, 5], "bbox_max": [1, 1, 1]},
            headers=auth_headers,
        )
        assert r.status_code in (400, 422)
