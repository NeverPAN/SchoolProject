"""重建任务全生命周期测试（Mock 管线 — 覆盖状态机/阶段事件/取消/恢复/模型登记）。"""
from __future__ import annotations

import time

import pytest


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


    def test_experiment_name_is_display_metadata(self, client, auth_headers, project, upload_images):
        upload_images(4)
        base = f"/api/v1/projects/{project['id']}"
        response = client.post(f"{base}/jobs", headers=auth_headers, json={"experiment_name": "  对照 · 双卡  "})
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["experiment_name"] == "对照 · 双卡"
        assert "experiment_name" not in job["config"]
        assert wait_job(client, auth_headers, job["id"])["status"] == "SUCCEEDED"
        assert client.get(f"{base}/jobs", headers=auth_headers).json()[0]["experiment_name"] == "对照 · 双卡"
        for model in client.get(f"{base}/models", headers=auth_headers).json():
            detail = client.get(f"/api/v1/models/{model['id']}", headers=auth_headers).json()
            assert detail["experiment_name"] == "对照 · 双卡" and detail["experiment"] is None
        invalid = client.post(f"{base}/jobs", headers=auth_headers, json={"experiment_name": "x" * 121})
        assert invalid.status_code == 422


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

    @pytest.mark.parametrize("upload_when", ["QUEUED", "RUNNING"])
    def test_late_upload_does_not_count_in_frozen_input(
        self, client, auth_headers, project, upload_images, monkeypatch, upload_when
    ):
        from app.reconstruction.mock_pipeline import MockPipeline
        from app.services import job_service
        from app.workers.jobs import run_reconstruction_job

        jobs_url = f"/api/v1/projects/{project['id']}/jobs"
        models_url = f"/api/v1/projects/{project['id']}/models"
        upload_images(4)
        r = client.post(jobs_url, json={"preset": "fast"}, headers=auth_headers)
        assert r.status_code == 202, r.text
        assert wait_job(client, auth_headers, r.json()["id"])["status"] == "SUCCEEDED"

        # Execute explicitly so the QUEUED upload cannot race with a worker claim.
        with monkeypatch.context() as patch:
            patch.setattr(job_service, "enqueue_job", lambda _job_id: None)
            r = client.post(jobs_url, json={"preset": "fast"}, headers=auth_headers)
            assert r.status_code == 202, r.text
            job_id = r.json()["id"]
            if upload_when == "QUEUED":
                assert r.json()["status"] == "QUEUED"
                upload_images(1, start_seed=100)
            else:
                run = MockPipeline.run

                def run_after_upload(pipeline):
                    status = client.get(f"/api/v1/jobs/{job_id}", headers=auth_headers).json()["status"]
                    assert status == "RUNNING"
                    upload_images(1, start_seed=100)
                    return run(pipeline)

                patch.setattr(MockPipeline, "run", run_after_upload)
            result = run_reconstruction_job(job_id)
            assert result["ok"], result

        models = client.get(models_url, headers=auth_headers).json()
        web = next(m for m in models if m["job_id"] == job_id and m["model_type"] == "web")
        version = client.get(f"/api/v1/models/{web['id']}", headers=auth_headers).json()["versions"][0]
        assert (version["version_number"], version["change_type"]) == (2, "rerun")

        # The next frozen snapshot does include the late upload, exactly once.
        r = client.post(jobs_url, json={"preset": "fast"}, headers=auth_headers)
        assert r.status_code == 202, r.text
        next_job = wait_job(client, auth_headers, r.json()["id"])
        assert next_job["status"] == "SUCCEEDED", next_job.get("error_message")
        models = client.get(models_url, headers=auth_headers).json()
        web = next(m for m in models if m["job_id"] == next_job["id"] and m["model_type"] == "web")
        next_version = client.get(f"/api/v1/models/{web['id']}", headers=auth_headers).json()["versions"][0]
        assert next_version["change_type"] == "append_images"
        assert next_version["parent_version_id"] == version["id"]
        assert "追加 1 张" in next_version["change_description"]

    @pytest.mark.parametrize("replace", [False, True], ids=["remove", "replace"])
    def test_removal_or_replacement_is_not_append(
        self, client, auth_headers, project, upload_images, replace
    ):
        jobs_url = f"/api/v1/projects/{project['id']}/jobs"
        images = upload_images(4).json()
        r = client.post(jobs_url, json={"preset": "fast"}, headers=auth_headers)
        assert r.status_code == 202, r.text
        assert wait_job(client, auth_headers, r.json()["id"])["status"] == "SUCCEEDED"

        removed = client.delete(f"/api/v1/images/{images[-1]['id']}", headers=auth_headers)
        assert removed.status_code == 200, removed.text
        if replace:
            upload_images(1, start_seed=100)
        r = client.post(jobs_url, json={"preset": "fast"}, headers=auth_headers)
        assert r.status_code == 202, r.text
        job = wait_job(client, auth_headers, r.json()["id"])
        assert job["status"] == "SUCCEEDED", job.get("error_message")
        models = client.get(f"/api/v1/projects/{project['id']}/models", headers=auth_headers).json()
        web = next(m for m in models if m["job_id"] == job["id"] and m["model_type"] == "web")
        version = client.get(f"/api/v1/models/{web['id']}", headers=auth_headers).json()["versions"][0]
        assert (version["version_number"], version["change_type"]) == (2, "inputs_changed")
        assert version["parent_version_id"] is not None

    @pytest.mark.parametrize("manifest", [None, "{", "{}", '{"images": [{"image_id": 1}]}'])
    def test_invalid_parent_snapshot_uses_unknown_attribution(self, manifest):
        from app.models.entities import ReconstructionJob
        from app.services.model_service import describe_job_version_change

        previous = ReconstructionJob(input_manifest=manifest)
        current = ReconstructionJob(id=2, input_manifest='{"images": [{"image_id": 1, "checksum": "abc"}]}')
        change_type, _ = describe_job_version_change(current, previous)
        assert change_type == "inputs_unknown"

    def test_failed_append_publication_preserves_previous_version(
        self, client, auth_headers, project, upload_images, monkeypatch
    ):
        from hashlib import sha256

        from sqlalchemy import select

        from app.db.session import SessionLocal
        from app.models.entities import ModelVersion
        from app.workers import jobs

        jobs_url = f"/api/v1/projects/{project['id']}/jobs"
        models_url = f"/api/v1/projects/{project['id']}/models"
        upload_images(4)
        r = client.post(jobs_url, json={"preset": "fast"}, headers=auth_headers)
        assert r.status_code == 202, r.text
        assert wait_job(client, auth_headers, r.json()["id"])["status"] == "SUCCEEDED"
        models_before = client.get(models_url, headers=auth_headers).json()
        web = next(m for m in models_before if m["model_type"] == "web")
        model_url = f"/api/v1/models/{web['id']}"
        version_before = client.get(model_url, headers=auth_headers).json()["versions"]
        manifest_before = client.get(f"{model_url}/manifest", headers=auth_headers).json()
        assert manifest_before["manifest"]
        download_before = client.get(f"{model_url}/download", headers=auth_headers)
        assert download_before.status_code == 200
        sha_before = sha256(download_before.content).hexdigest()
        with SessionLocal() as db:
            version_ids = set(db.scalars(select(ModelVersion.id).filter_by(project_id=project["id"])))

        upload_images(1, start_seed=100)

        def fail_manifest_write(path, payload):
            raise OSError("injected update publication failure")

        with monkeypatch.context() as patch:
            # Fail after new model/version rows are flushed, but before their transaction commits.
            patch.setattr(jobs, "atomic_write_json", fail_manifest_write)
            r = client.post(jobs_url, json={"preset": "fast"}, headers=auth_headers)
            assert r.status_code == 202, r.text
            failed = wait_job(client, auth_headers, r.json()["id"])
            assert failed["status"] == "FAILED"
            assert "injected update publication failure" in failed["error_message"]

        assert client.get(models_url, headers=auth_headers).json() == models_before
        assert client.get(model_url, headers=auth_headers).json()["versions"] == version_before
        for endpoint in ("file", "download"):
            old_artifact = client.get(f"{model_url}/{endpoint}", headers=auth_headers)
            assert old_artifact.status_code == 200
            assert sha256(old_artifact.content).hexdigest() == sha_before
        with SessionLocal() as db:
            assert set(db.scalars(select(ModelVersion.id).filter_by(project_id=project["id"]))) == version_ids
        assert client.get(f"{model_url}/manifest", headers=auth_headers).json() == manifest_before
        retry = client.post(f"/api/v1/jobs/{failed['id']}/resume", headers=auth_headers)
        assert retry.status_code < 300, retry.text
        assert wait_job(client, auth_headers, failed["id"])["status"] == "SUCCEEDED"
        published = client.get(models_url, headers=auth_headers).json()
        successors = [m for m in published if m["job_id"] == failed["id"] and m["model_type"] == "web"]
        assert len(successors) == 1
        version = client.get(f"/api/v1/models/{successors[0]['id']}", headers=auth_headers).json()["versions"]
        assert len(version) == 1
        assert (version[0]["parent_version_id"], version[0]["version_number"], version[0]["change_type"]) == (
            version_before[0]["id"], 2, "append_images"
        )


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
        detail = client.get(f"/api/v1/models/{local['id']}", headers=auth_headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["local_extraction"] == {
            "parent_model_id": web["id"], "region_id": region["id"],
            "bounds": {"min": region["bbox_min"], "max": region["bbox_max"]},
        }
        assert client.get(f"/api/v1/models/{web['id']}", headers=auth_headers).json()["local_extraction"] is None
        assert any(e["stage"] == "LOCAL_UPDATE" and e["status"] == "completed" for e in job2["events"])
        assert "区域提取完成" in client.get(f"/api/v1/jobs/{job2['id']}/logs", headers=auth_headers).text

        # C：提取报告如实记录裁剪语义、父文件哈希、边界与贴图继承状态
        import hashlib
        import json
        from pathlib import Path

        from app.core.config import get_settings

        report = json.loads((Path(get_settings().data_dir) / "users" / str(project["owner_id"]) / "projects"
                             / str(project["id"]) / "outputs" / f"job-{job2['id']}" / "region_report.json").read_text())
        assert report["crop_semantics"] == "triangle-centroid"
        assert report["region_faces"] == local["face_count"]
        assert report["boundary_edges"] >= 0 and isinstance(report["boundary_open"], bool)
        from app.db.session import SessionLocal
        from app.models.entities import Model

        with SessionLocal() as db:
            parent_path = db.get(Model, web["id"]).file_path
        assert report["parent_sha256"] == hashlib.sha256(Path(parent_path).read_bytes()).hexdigest()
        assert report["texture_assets"]["primitives_total"] > 0

    @pytest.mark.parametrize("case", [
        "missing", "malformed_json", "array_manifest", "wrong_job", "null_job", "float_job",
        "wrong_mode", "wrong_stage", "missing_parent", "cross_project_parent", "self_parent",
        "nonpositive_parent_id", "huge_parent_id", "bool_region_id", "string_region_id",
        "missing_region_id", "missing_params", "short_bounds", "unordered_bounds", "flat_bounds",
        "infinite_bounds", "nan_bounds", "bool_bounds", "string_bounds", "huge_bounds",
    ])
    def test_unverified_local_extraction_returns_null(self, client, auth_headers, project, tmp_path, case):
        import json

        from app.db.session import SessionLocal
        from app.models.entities import Model, Project, ReconstructionJob

        artifact = tmp_path / "region.glb"
        with SessionLocal() as db:
            other_project = Project(name="Another project", owner_id=project["owner_id"])
            job = ReconstructionJob(project_id=project["id"], pipeline_mode="local_update")
            db.add_all([other_project, job])
            db.flush()
            parent = Model(project_id=project["id"], model_type="web", format="glb", file_path=str(tmp_path / "parent.glb"))
            other_parent = Model(project_id=other_project.id, model_type="web", format="glb", file_path=str(tmp_path / "other.glb"))
            local = Model(
                project_id=project["id"], model_type="mesh", format="glb", file_path=str(artifact),
                job_id=None if case == "null_job" else job.id,
                stage_origin="LOCAL_POINT_FUSION" if case == "wrong_stage" else "LOCAL_REGION",
            )
            db.add_all([parent, other_parent, local])
            db.flush()
            parent_id, other_parent_id, local_id, job_id = parent.id, other_parent.id, local.id, job.id
            db.commit()

        manifest = {
            "job_id": job_id, "pipeline_mode": "local_update",
            "target_model_id": parent_id, "region_id": 1,
            "resolved_params": {"bbox_min": [6.48, -0.75, 2.46], "bbox_max": [7.95, -0.38, 3.17]},
            "outputs": [str(artifact)],
        }
        overrides = {
            "wrong_job": {"job_id": job_id + 1}, "float_job": {"job_id": float(job_id)},
            "null_job": {"job_id": None}, "wrong_mode": {"pipeline_mode": "local_reconstruct"},
            "missing_parent": {"target_model_id": max(parent_id, other_parent_id, local_id) + 1},
            "cross_project_parent": {"target_model_id": other_parent_id},
            "self_parent": {"target_model_id": local_id}, "nonpositive_parent_id": {"target_model_id": 0},
            "huge_parent_id": {"target_model_id": 10**30}, "bool_region_id": {"region_id": True},
            "string_region_id": {"region_id": "1"}, "missing_region_id": {"region_id": None},
            "missing_params": {"resolved_params": []},
        }
        manifest.update(overrides.get(case, {}))
        invalid_min = {
            "short_bounds": [0, 0], "unordered_bounds": [9, 0, 0], "flat_bounds": [7.95, -0.75, 2.46],
            "infinite_bounds": [float("-inf"), 0, 0], "nan_bounds": [float("nan"), 0, 0],
            "bool_bounds": [True, -0.75, 2.46], "string_bounds": ["6.48", -0.75, 2.46],
            "huge_bounds": [-(10**350), -0.75, 2.46],
        }
        if case in invalid_min:
            manifest["resolved_params"]["bbox_min"] = invalid_min[case]
        if case != "missing":
            content = {"malformed_json": "{", "array_manifest": "[]"}.get(case, json.dumps(manifest))
            artifact.with_name("job_manifest.json").write_text(content, encoding="utf-8")

        detail = client.get(f"/api/v1/models/{local_id}", headers=auth_headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["local_extraction"] is None

    def test_local_reconstruction_freezes_calibrated_inputs(
        self, client, auth_headers, auth_headers_b, project, upload_images, monkeypatch
    ):
        from app.db.session import SessionLocal
        from app.models.entities import ReconstructionJob
        from app.services import job_service
        import json

        upload_images(4)
        base = f"/api/v1/projects/{project['id']}"
        result = client.post(f"{base}/jobs", headers=auth_headers, json={})
        assert result.status_code == 202
        assert wait_job(client, auth_headers, result.json()["id"])["status"] == "SUCCEEDED"
        ids = client.get(f"{base}/images", headers=auth_headers).json()
        ids = [i["id"] for i in ids][:3]
        web = next(m for m in client.get(f"{base}/models", headers=auth_headers).json() if m["model_type"] == "web")
        model = f"/api/v1/models/{web['id']}"
        with SessionLocal() as db:
            parent = db.get(ReconstructionJob, result.json()["id"])
            expected = {e["filename"]:e["checksum"] for e in json.loads(parent.input_manifest)["images"] if e["image_id"] in ids}
        seed = dict(bbox_min=[-2]*3, bbox_max=[2]*3, new_view_sha256=expected)
        monkeypatch.setattr(job_service, "local_seed", lambda m: dict(seed))
        saved = client.post(model+"/regions", headers=auth_headers, json={"bbox_min":[-1]*3,"bbox_max":[1]*3})
        assert saved.status_code == 201
        endpoint = model+"/local-reconstruction"
        body = dict(region_id=saved.json()["id"], image_ids=ids)
        assert client.post(endpoint, headers=auth_headers_b, json=body).status_code == 404
        assert client.post(endpoint, headers=auth_headers, json={**body,"image_ids":[ids[0]]*3}).status_code == 400
        pending = []
        monkeypatch.setattr(job_service, "enqueue_job", pending.append)
        started = client.post(endpoint, headers=auth_headers, json=body)
        assert started.status_code == 202, started.text
        assert pending == [started.json()["id"]]
        with SessionLocal() as db:
            job = db.get(ReconstructionJob, pending[0])
            assert job.pipeline_mode == "local_reconstruct"
            assert {e["image_id"] for e in json.loads(job.input_manifest)["images"]} == set(ids)
            assert json.loads(job.config_json)["target_model_id"] == web["id"]
        assert client.post(endpoint, headers=auth_headers, json=body).status_code == 409

    def test_region_queue_failure_allows_retry_with_same_region(
        self, client, auth_headers, project, upload_images, monkeypatch
    ):
        from app.services import job_service

        upload_images(4)
        result = client.post(f"/api/v1/projects/{project['id']}/jobs", json={}, headers=auth_headers)
        assert result.status_code == 202, result.text
        assert wait_job(client, auth_headers, result.json()["id"])["status"] == "SUCCEEDED"
        models = client.get(f"/api/v1/projects/{project['id']}/models", headers=auth_headers).json()
        web = next(m for m in models if m["model_type"] == "web")
        url = f"/api/v1/models/{web['id']}"
        region = client.post(f"{url}/regions", json={
            "bbox_min": [-100, -100, -100], "bbox_max": [100, 100, 100],
        }, headers=auth_headers)
        assert region.status_code == 201, region.text
        payload = {"region_id": region.json()["id"]}
        attempted = []

        def queue_unavailable(job_id):
            attempted.append(job_id)
            raise ConnectionError("isolated queue unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(job_service, "enqueue_job", queue_unavailable)
            failed = client.post(f"{url}/local-update", json=payload, headers=auth_headers)
        assert failed.status_code == 409, failed.text
        assert failed.json()["code"] == "QUEUE_UNAVAILABLE"
        assert len(attempted) == 1
        job = client.get(f"/api/v1/jobs/{attempted[0]}", headers=auth_headers).json()
        assert (job["status"], job["error_code"]) == ("FAILED", "QUEUE_UNAVAILABLE")
        retried = client.post(f"{url}/local-update", json=payload, headers=auth_headers)
        assert retried.status_code == 202, retried.text
        assert wait_job(client, auth_headers, retried.json()["id"])["status"] == "SUCCEEDED"
        assert client.get(f"{url}/regions", headers=auth_headers).json() == [region.json()]

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
