"""pytest 全局 fixtures — 独立临时环境（隔离 DB/存储/Redis DB）。"""
from __future__ import annotations

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

# ---- 在导入 app 之前设置测试环境 ----
_TMP = tempfile.mkdtemp(prefix="recon-test-")
os.environ["RECON_ENV_FILE"] = "/nonexistent/.env"  # 屏蔽真实 .env
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["DATA_DIR"] = f"{_TMP}/data"
os.environ["SCRATCH_DIR"] = f"{_TMP}/scratch"
os.environ["RECON_MODE"] = "mock"
os.environ["MOCK_DELAY_SCALE"] = "0.01"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/15"
os.environ["LOG_LEVEL"] = "WARNING"

from fastapi.testclient import TestClient  # noqa: E402

from app.db.session import create_all  # noqa: E402
from app.main import app  # noqa: E402

_executor = ThreadPoolExecutor(max_workers=2)


@pytest.fixture(scope="session", autouse=True)
def _setup_db():
    create_all()
    yield


@pytest.fixture()
def client(monkeypatch):
    """TestClient；mock 模式下将 RQ 入队替换为进程内线程执行真实 Worker 函数。"""
    from app.services import job_service

    def fake_enqueue(job_id: int):
        _executor.submit(_run_worker_job, job_id)

    monkeypatch.setattr(job_service, "enqueue_job", fake_enqueue)
    with TestClient(app) as c:
        yield c


def _run_worker_job(job_id: int):
    from app.workers.jobs import run_reconstruction_job

    run_reconstruction_job(job_id)


@pytest.fixture()
def auth_headers(client):
    """注册并登录一个测试用户，返回 Authorization 头。"""
    r = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "password": "secret123"},
    )
    if r.status_code == 409:
        pass  # 已存在
    r = client.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def auth_headers_b(client):
    """第二个用户（隔离性测试用）。"""
    client.post("/api/v1/auth/register", json={"username": "bob", "password": "secret456"})
    r = client.post("/api/v1/auth/login", json={"username": "bob", "password": "secret456"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def project(client, auth_headers):
    r = client.post(
        "/api/v1/projects",
        json={"name": "测试项目", "description": "pytest 项目"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def make_image_bytes(width=320, height=240, color=(120, 140, 160), fmt="JPEG", seed=0):
    import io

    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    arr = np.full((height, width, 3), color, dtype=np.float32)
    arr += rng.normal(0, 10, arr.shape)
    # 加一点纹理避免被判为"无特征"
    xx, yy = np.mgrid[0:height, 0:width]
    arr[..., 0] += ((xx // 16 + yy // 16) % 2) * 40
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    im.save(buf, fmt)
    return buf.getvalue()


@pytest.fixture()
def upload_images(client, auth_headers, project):
    """向项目上传 5 张有效图片，返回图片列表响应。"""

    def _upload(n=5, start_seed=0):
        files = []
        for i in range(n):
            b = make_image_bytes(seed=start_seed + i)
            files.append(("files", (f"view_{start_seed + i:03d}.jpg", b, "image/jpeg")))
        r = client.post(
            f"/api/v1/projects/{project['id']}/images",
            files=files,
            headers=auth_headers,
        )
        assert r.status_code == 201, r.text
        return r

    return _upload
