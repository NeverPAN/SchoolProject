"""项目 CRUD 与用户隔离测试。"""
from __future__ import annotations


class TestProjectCRUD:
    def test_create_and_get(self, client, auth_headers, project):
        r = client.get(f"/api/v1/projects/{project['id']}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["name"] == "测试项目"
        assert r.json()["image_count"] == 0

    def test_list_projects(self, client, auth_headers, project):
        r = client.get("/api/v1/projects", headers=auth_headers)
        assert r.status_code == 200
        assert any(p["id"] == project["id"] for p in r.json())

    def test_update_project(self, client, auth_headers, project):
        r = client.patch(
            f"/api/v1/projects/{project['id']}",
            json={"description": "更新后的描述"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json()["description"] == "更新后的描述"

    def test_delete_project(self, client, auth_headers, project):
        pid = project["id"]
        r = client.delete(f"/api/v1/projects/{pid}", headers=auth_headers)
        assert r.status_code == 200
        r = client.get(f"/api/v1/projects/{pid}", headers=auth_headers)
        assert r.status_code == 404


class TestOwnershipIsolation:
    """用户 A 不能通过修改 URL 访问用户 B 的项目（模块 A 验收）。"""

    def test_b_cannot_read_a_project(self, client, auth_headers, auth_headers_b, project):
        r = client.get(f"/api/v1/projects/{project['id']}", headers=auth_headers_b)
        assert r.status_code == 404  # 不泄露存在性

    def test_b_cannot_update_a_project(self, client, auth_headers_b, project):
        r = client.patch(
            f"/api/v1/projects/{project['id']}", json={"name": "hacked"}, headers=auth_headers_b
        )
        assert r.status_code == 404

    def test_b_cannot_delete_a_project(self, client, auth_headers_b, project):
        r = client.delete(f"/api/v1/projects/{project['id']}", headers=auth_headers_b)
        assert r.status_code == 404

    def test_b_cannot_upload_to_a_project(self, client, auth_headers_b, project):
        from tests.conftest import make_image_bytes

        r = client.post(
            f"/api/v1/projects/{project['id']}/images",
            files=[("files", ("x.jpg", make_image_bytes(), "image/jpeg"))],
            headers=auth_headers_b,
        )
        assert r.status_code == 404

    def test_b_cannot_create_job_on_a_project(self, client, auth_headers_b, project):
        r = client.post(
            f"/api/v1/projects/{project['id']}/jobs",
            json={"preset": "fast"},
            headers=auth_headers_b,
        )
        assert r.status_code == 404


class TestValidation:
    def test_empty_name_rejected(self, client, auth_headers):
        r = client.post("/api/v1/projects", json={"name": ""}, headers=auth_headers)
        assert r.status_code == 422

    def test_project_requires_auth(self, client):
        assert client.get("/api/v1/projects").status_code == 401
